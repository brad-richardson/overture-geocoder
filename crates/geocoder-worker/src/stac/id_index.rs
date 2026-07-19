//! Parquet ID-index reader: range-read UUID lookup plus locator dictionary
//! validation and metadata assembly.

use std::cell::RefCell;
use std::collections::HashMap;
use std::rc::Rc;

use bytes::Bytes;
use geocoder_core::{IdLocatorMetadata, IdLookupResult};
use parquet::file::reader::{ChunkReader, FileReader, Length, SerializedFileReader};
use parquet::record::RowAccessor;
use serde::Deserialize;
use sha2::{Digest, Sha256};
use worker::*;

use super::cache::{ID_INDEX_CACHE_TTL, IMMUTABLE_CACHE_TTL};
use super::catalog::with_version_fallback;
use super::{not_found, ShardLoader};

const LOCATOR_DICTIONARY_CACHE_MAX_ENTRIES: usize = 2;
const MAX_PARQUET_FOOTER_SIZE: usize = 16 * 1024 * 1024;

thread_local! {
    /// Parsed immutable locator dictionaries. Raw JSON is separately edge-
    /// cached; this avoids reparsing and reallocating ~100 KiB on every hit.
    static LOCATOR_DICTIONARY_CACHE: RefCell<Vec<(String, Rc<LocatorDictionary>)>> =
        const { RefCell::new(Vec::new()) };
}

pub struct IdSearchResult {
    pub result: Option<IdLookupResult>,
    pub version: String,
}

#[derive(Debug, Clone)]
struct IdIndexConfig {
    prefix_len: usize,
    format_version: u32,
    overture_release: Option<String>,
    locator_dictionary: Option<LocatorDictionaryReference>,
}

#[derive(Debug, Clone, Deserialize)]
struct LocatorDictionaryReference {
    href: String,
    sha256: String,
    size_bytes: usize,
    dictionary_version: u32,
    source_files_count: usize,
    last_seen_releases_count: usize,
    source_file_id_bounds: Option<[u32; 2]>,
    last_seen_release_id_bounds: Option<[u32; 2]>,
}

#[derive(Debug, Clone, Deserialize, PartialEq, Eq, PartialOrd, Ord)]
struct SourceFileEntry {
    theme: String,
    feature_type: String,
    filename: String,
}

#[derive(Debug, Clone, Deserialize)]
struct LocatorDictionary {
    format_version: u32,
    dictionary_version: u32,
    overture_release: String,
    type_theme_map: TypeThemeMap,
    source_files: Vec<SourceFileEntry>,
    last_seen_releases: Vec<String>,
    source_files_count: usize,
    last_seen_releases_count: usize,
    source_file_id_bounds: Option<[u32; 2]>,
    last_seen_release_id_bounds: Option<[u32; 2]>,
}

#[derive(Debug, Clone, Deserialize)]
struct TypeThemeMap {
    version: u32,
    types: HashMap<String, String>,
}

fn parse_id_index_config(text: &str) -> std::result::Result<IdIndexConfig, String> {
    let root: serde_json::Value =
        serde_json::from_str(text).map_err(|e| format!("invalid JSON: {e}"))?;
    let values = root.get("summaries").unwrap_or(&root);
    let prefix_len = values
        .get("prefix_len")
        .and_then(|v| v.as_u64())
        .ok_or_else(|| "missing integer prefix_len".to_string())? as usize;
    let format_version = match values.get("format_version") {
        None => 1,
        Some(value) => value
            .as_u64()
            .and_then(|value| u32::try_from(value).ok())
            .ok_or_else(|| "format_version must be an unsigned 32-bit integer".to_string())?,
    };

    let (overture_release, locator_dictionary) = if format_version == 3 {
        let release = values
            .get("overture_release")
            .and_then(|value| value.as_str())
            .filter(|value| !value.is_empty())
            .ok_or_else(|| "format v3 requires overture_release".to_string())?
            .to_string();
        let reference: LocatorDictionaryReference = serde_json::from_value(
            values
                .get("locator_dictionary")
                .cloned()
                .ok_or_else(|| "format v3 requires locator_dictionary".to_string())?,
        )
        .map_err(|e| format!("invalid locator dictionary reference: {e}"))?;
        validate_dictionary_reference(&reference)?;
        (Some(release), Some(reference))
    } else if format_version == 1 {
        (None, None)
    } else {
        return Err(format!(
            "unsupported ID-index format_version {format_version}"
        ));
    };

    Ok(IdIndexConfig {
        prefix_len,
        format_version,
        overture_release,
        locator_dictionary,
    })
}

fn validate_dictionary_reference(
    reference: &LocatorDictionaryReference,
) -> std::result::Result<(), String> {
    let expected_href = format!("./id-locator-dictionary-{}.json", reference.sha256);
    if reference.dictionary_version != 1
        || reference.sha256.len() != 64
        || !reference
            .sha256
            .bytes()
            .all(|byte| b"0123456789abcdef".contains(&byte))
        || reference.href != expected_href
        || reference.size_bytes == 0
        || reference.size_bytes > 1024 * 1024
        || reference.source_files_count > 65_535
        || reference.last_seen_releases_count > 65_535
    {
        return Err("malformed locator dictionary reference".to_string());
    }
    validate_bounds(
        reference.source_file_id_bounds,
        reference.source_files_count,
    )?;
    validate_bounds(
        reference.last_seen_release_id_bounds,
        reference.last_seen_releases_count,
    )?;
    Ok(())
}

fn validate_bounds(bounds: Option<[u32; 2]>, count: usize) -> std::result::Result<(), String> {
    let expected = if count == 0 {
        None
    } else {
        Some([1, count as u32])
    };
    if bounds != expected {
        return Err("locator dictionary bounds/count mismatch".to_string());
    }
    Ok(())
}

fn parquet_footer_metadata_len(tail_bytes: &[u8]) -> std::result::Result<usize, String> {
    if tail_bytes.len() < 8 {
        return Err("shard too small for parquet footer".to_string());
    }
    let trailer_start = tail_bytes.len() - 8;
    if &tail_bytes[trailer_start + 4..] != b"PAR1" {
        return Err("invalid parquet magic".to_string());
    }
    Ok(u32::from_le_bytes(
        tail_bytes[trailer_start..trailer_start + 4]
            .try_into()
            .expect("four-byte parquet footer length"),
    ) as usize)
}

/// Validate a footer length read from Parquet's final eight bytes and decide
/// whether the Worker's initial suffix needs one exact-size retry.
///
/// `tail_len` is the number of bytes actually returned by the initial suffix
/// request (which can be smaller than 32 KiB for a small object).
fn footer_retry_size(
    file_size: u64,
    tail_len: usize,
    metadata_len: usize,
) -> std::result::Result<Option<u64>, String> {
    let footer_size = metadata_len
        .checked_add(8)
        .ok_or_else(|| "parquet footer length overflow".to_string())?;
    if tail_len as u64 > file_size
        || metadata_len > MAX_PARQUET_FOOTER_SIZE
        || footer_size as u64 > file_size
    {
        return Err(format!(
            "implausible parquet footer length {metadata_len}B for {file_size}B file"
        ));
    }
    Ok((footer_size > tail_len).then_some(footer_size as u64))
}

fn validate_footer_retry_response(
    expected_file_size: u64,
    expected_metadata_len: usize,
    actual_file_size: u64,
    tail_bytes: &[u8],
) -> std::result::Result<(), String> {
    if actual_file_size != expected_file_size {
        return Err("parquet object changed size between footer reads".to_string());
    }
    let actual_metadata_len = parquet_footer_metadata_len(tail_bytes)?;
    if actual_metadata_len != expected_metadata_len {
        return Err("parquet footer changed between suffix reads".to_string());
    }
    if footer_retry_size(actual_file_size, tail_bytes.len(), actual_metadata_len)?.is_some() {
        return Err("exact parquet footer retry returned too few bytes".to_string());
    }
    Ok(())
}

fn compact_locator_ids(
    row: &parquet::record::Row,
    format_version: u32,
) -> Option<(Option<u16>, Option<u16>, bool)> {
    if format_version != 3 || row.len() < 8 {
        return None;
    }
    let source_raw = row.get_int(5).ok();
    let release_raw = row.get_int(6).ok();
    // Check physical nullity before conversion. Otherwise a corrupt negative
    // or overflowing value could be discarded and make a both-present row
    // look like a valid current or historical locator.
    if source_raw.is_some() == release_raw.is_some() {
        return None;
    }
    let source_file_id = source_raw
        .and_then(|value| u16::try_from(value).ok())
        .filter(|value| *value != 0);
    let last_seen_release_id = release_raw
        .and_then(|value| u16::try_from(value).ok())
        .filter(|value| *value != 0);
    if source_file_id.is_none() && last_seen_release_id.is_none() {
        return None;
    }
    let registry_member = row.get_bool(7).ok()?;
    Some((source_file_id, last_seen_release_id, registry_member))
}

fn validate_locator_dictionary(
    dictionary: &LocatorDictionary,
    reference: &LocatorDictionaryReference,
    expected_release: &str,
) -> std::result::Result<(), String> {
    if dictionary.format_version != 3
        || dictionary.dictionary_version != 1
        || dictionary.overture_release != expected_release
        || dictionary.type_theme_map.version != 1
        || dictionary.source_files.len() != dictionary.source_files_count
        || dictionary.last_seen_releases.len() != dictionary.last_seen_releases_count
        || dictionary.source_files_count != reference.source_files_count
        || dictionary.last_seen_releases_count != reference.last_seen_releases_count
    {
        return Err("locator dictionary contract mismatch".to_string());
    }
    validate_bounds(
        dictionary.source_file_id_bounds,
        dictionary.source_files_count,
    )?;
    validate_bounds(
        dictionary.last_seen_release_id_bounds,
        dictionary.last_seen_releases_count,
    )?;
    if dictionary.source_file_id_bounds != reference.source_file_id_bounds
        || dictionary.last_seen_release_id_bounds != reference.last_seen_release_id_bounds
    {
        return Err("locator dictionary reference bounds mismatch".to_string());
    }
    if dictionary
        .source_files
        .windows(2)
        .any(|pair| pair[0] >= pair[1])
        || dictionary
            .last_seen_releases
            .windows(2)
            .any(|pair| pair[0] >= pair[1])
    {
        return Err("locator dictionaries are not strictly sorted and unique".to_string());
    }
    for entry in &dictionary.source_files {
        if entry.filename.is_empty()
            || entry.filename.len() > 255
            || entry.filename.contains('/')
            || entry.filename.contains('\\')
            || !entry.filename.ends_with(".parquet")
            || dictionary.type_theme_map.types.get(&entry.feature_type) != Some(&entry.theme)
        {
            return Err("invalid source-file dictionary entry".to_string());
        }
    }
    if dictionary.last_seen_releases.iter().any(String::is_empty) {
        return Err("invalid last-seen release dictionary entry".to_string());
    }
    Ok(())
}

fn build_locator_metadata(
    source_file_id: Option<u16>,
    last_seen_release_id: Option<u16>,
    registry_member: bool,
    dictionary: &LocatorDictionary,
) -> Option<IdLocatorMetadata> {
    let (
        feature_type,
        theme,
        filename,
        last_seen_release,
        exists_in_current_release,
        overture_path,
    ) = if let Some(id) = source_file_id {
        let entry = dictionary.source_files.get(usize::from(id) - 1)?;
        let path = format!(
            "release/{}/theme={}/type={}/{}",
            dictionary.overture_release, entry.theme, entry.feature_type, entry.filename
        );
        (
            Some(entry.feature_type.clone()),
            Some(entry.theme.clone()),
            Some(entry.filename.clone()),
            Some(dictionary.overture_release.clone()),
            true,
            Some(path),
        )
    } else {
        let id = last_seen_release_id?;
        let release = dictionary
            .last_seen_releases
            .get(usize::from(id) - 1)?
            .clone();
        (None, None, None, Some(release), false, None)
    };

    Some(IdLocatorMetadata {
        feature_type,
        theme,
        filename,
        last_seen_release,
        registry_member,
        exists_in_current_release,
        overture_path,
    })
}

/// Parse a GERS ID string into 16 UUID bytes.
///
/// Accepts both hyphenated ("08b2a100-d664-...") and plain ("08b2a100d664...") formats.
fn parse_uuid_bytes(gers_id: &str) -> Option<[u8; 16]> {
    let hex: String = gers_id.chars().filter(|c| *c != '-').collect();
    if hex.len() != 32 {
        return None;
    }
    let mut bytes = [0u8; 16];
    for i in 0..16 {
        bytes[i] = u8::from_str_radix(&hex[i * 2..i * 2 + 2], 16).ok()?;
    }
    Some(bytes)
}

/// A ChunkReader backed by pre-fetched byte ranges from R2.
///
/// Allows the standard parquet reader to operate on non-contiguous file regions
/// (e.g., a footer from the end of file + a single row group from the middle)
/// without fetching the entire file.
struct RangeChunkReader {
    file_size: u64,
    /// Pre-fetched ranges: (absolute_offset_in_file, data)
    chunks: Vec<(u64, Bytes)>,
}

impl Length for RangeChunkReader {
    fn len(&self) -> u64 {
        self.file_size
    }
}

impl ChunkReader for RangeChunkReader {
    type T = std::io::Cursor<Bytes>;

    fn get_read(&self, start: u64) -> parquet::errors::Result<Self::T> {
        for (offset, data) in &self.chunks {
            if start >= *offset && start < *offset + data.len() as u64 {
                let local_start = (start - *offset) as usize;
                return Ok(std::io::Cursor::new(data.slice(local_start..)));
            }
        }
        Err(parquet::errors::ParquetError::General(format!(
            "Range not pre-fetched: offset {}",
            start
        )))
    }

    fn get_bytes(&self, start: u64, length: usize) -> parquet::errors::Result<Bytes> {
        let end = start + length as u64;
        for (offset, data) in &self.chunks {
            let chunk_end = *offset + data.len() as u64;
            if start >= *offset && end <= chunk_end {
                let local_start = (start - *offset) as usize;
                return Ok(data.slice(local_start..local_start + length));
            }
        }
        Err(parquet::errors::ParquetError::General(format!(
            "Range not pre-fetched: {}..{}",
            start, end
        )))
    }
}

impl ShardLoader {
    /// Look up a GERS ID to get its bounding box from a parquet shard.
    /// Falls back to older versions if the latest version's index is unavailable.
    pub async fn lookup_id(&self, gers_id: &str) -> Result<IdSearchResult> {
        with_version_fallback!(self, "id_lookup", version, {
            self.try_lookup_id(version, gers_id).await
        })
    }

    /// Look up an ID in the core release selected by the atomic v2 manifest.
    pub(crate) async fn lookup_id_version(
        &self,
        version: &str,
        gers_id: &str,
    ) -> Result<IdSearchResult> {
        self.try_lookup_id(version, gers_id).await
    }

    /// Attempt ID lookup against a specific version using range reads.
    ///
    /// Instead of fetching the entire parquet shard (~28 MB for prefix-len 3),
    /// this reads only the footer metadata + one matching row group (~1-3 MB):
    /// 1. Suffix read (32 KB) → get file size + parquet footer
    /// 2. Parse footer → find row group containing the target UUID via min/max stats
    /// 3. Range read just that row group's column data
    async fn try_lookup_id(&self, version: &str, gers_id: &str) -> Result<IdSearchResult> {
        let id_config = self.load_id_index_config(version).await?;
        let prefix_len = id_config.prefix_len;

        let hex_id: String = gers_id.replace('-', "").to_lowercase();
        let Some(prefix) = hex_id.get(..prefix_len) else {
            return Ok(IdSearchResult {
                result: None,
                version: version.to_string(),
            });
        };
        let shard_key = format!("{}/id-index/{}.parquet", version, prefix);

        let target = match parse_uuid_bytes(gers_id) {
            Some(t) => t,
            None => {
                return Ok(IdSearchResult {
                    result: None,
                    version: version.to_string(),
                })
            }
        };

        // Step 1: Suffix read to get footer + file size (cached at edge).
        // Missing shard is reported as a retriable error (not Ok(None)) so the
        // version-fallback macro retries the prior version. This handles the
        // window where catalog.json points at a new version whose id-index
        // parquets haven't finished uploading yet.
        const FOOTER_SUFFIX_SIZE: u64 = 32768;
        let (file_size, mut tail_bytes) = match self
            .cached_suffix_read(&shard_key, FOOTER_SUFFIX_SIZE)
            .await?
        {
            Some(result) => result,
            None => return Err(not_found(format!("id-index shard {}", shard_key))),
        };

        // Step 2: Parse parquet footer from the tail bytes
        let metadata_len = parquet_footer_metadata_len(&tail_bytes).map_err(|reason| {
            Error::RustError(format!(
                "Invalid parquet footer for {}: {}",
                shard_key, reason
            ))
        })?;
        // Sanity-cap before acting on the length: a corrupt (or stale-cached)
        // 4-byte footer field of up to ~4 GB would otherwise trigger a
        // whole-file suffix fetch, buffered in memory and edge-cached.
        let footer_retry =
            footer_retry_size(file_size, tail_bytes.len(), metadata_len).map_err(|reason| {
                Error::RustError(format!(
                    "Invalid parquet footer for {}: {}",
                    shard_key, reason
                ))
            })?;
        if let Some(footer_size) = footer_retry {
            // Footer larger than the default suffix window: re-read with the
            // exact size (cached under a size-specific key).
            console_log!(
                "Footer {}B exceeds {}B window for {}, re-reading",
                footer_size,
                tail_bytes.len(),
                shard_key
            );
            tail_bytes = match self.cached_suffix_read(&shard_key, footer_size).await? {
                Some((retry_file_size, bytes)) => {
                    validate_footer_retry_response(
                        file_size,
                        metadata_len,
                        retry_file_size,
                        &bytes,
                    )
                    .map_err(|reason| {
                        Error::RustError(format!(
                            "Invalid parquet footer retry for {}: {}",
                            shard_key, reason
                        ))
                    })?;
                    bytes
                }
                None => return Err(not_found(format!("id-index shard {}", shard_key))),
            };
        }
        let tail_len = tail_bytes.len() as u64;
        let tail_offset = file_size.checked_sub(tail_len).ok_or_else(|| {
            Error::RustError(format!(
                "Parquet suffix for {} exceeds object size",
                shard_key
            ))
        })?;
        let metadata_start = tail_bytes.len() - 8 - metadata_len;
        let metadata = parquet::file::metadata::ParquetMetaDataReader::decode_metadata(
            &tail_bytes[metadata_start..metadata_start + metadata_len],
        )
        .map_err(|e| Error::RustError(format!("Bad parquet metadata: {}", e)))?;

        // Step 3: Find matching row group via UUID column min/max statistics
        let num_row_groups = metadata.num_row_groups();
        let mut matching_rg: Option<usize> = None;
        for rg_idx in 0..num_row_groups {
            let rg_meta = metadata.row_group(rg_idx);
            if let Some(stats) = rg_meta.column(0).statistics() {
                if let (Some(min), Some(max)) = (stats.min_bytes_opt(), stats.max_bytes_opt()) {
                    if target.as_slice() >= min && target.as_slice() <= max {
                        matching_rg = Some(rg_idx);
                        break;
                    }
                }
            }
        }
        let rg_idx = match matching_rg {
            Some(idx) => idx,
            None => {
                return Ok(IdSearchResult {
                    result: None,
                    version: version.to_string(),
                })
            }
        };

        // Step 4: Compute byte range for the matching row group's columns
        let rg_meta = metadata.row_group(rg_idx);
        let num_columns = rg_meta.num_columns();
        let mut rg_start = u64::MAX;
        let mut rg_end = 0u64;
        for col_idx in 0..num_columns {
            let col_meta = rg_meta.column(col_idx);
            let col_offset = col_meta
                .dictionary_page_offset()
                .map(|o| o as u64)
                .unwrap_or(col_meta.data_page_offset() as u64);
            let col_end = col_offset + col_meta.compressed_size() as u64;
            rg_start = rg_start.min(col_offset);
            rg_end = rg_end.max(col_end);
        }
        let rg_length = rg_end - rg_start;

        console_log!(
            "ID lookup: shard={} file={}B rg={}/{} range={}..{} ({}B)",
            prefix,
            file_size,
            rg_idx,
            num_row_groups,
            rg_start,
            rg_end,
            rg_length
        );

        // Step 5: Fetch row group data (may already be in our tail buffer)
        let rg_bytes = if rg_start >= tail_offset && rg_end <= tail_offset + tail_bytes.len() as u64
        {
            // Row group is within our already-fetched tail (small file or last row group)
            let local_start = (rg_start - tail_offset) as usize;
            tail_bytes.slice(local_start..local_start + rg_length as usize)
        } else {
            // Range read just this row group, edge-cached: bulk ID resolvers
            // hit the same row group repeatedly and shouldn't re-pay R2.
            self.cached_range_read(&shard_key, rg_start, rg_length)
                .await?
                .ok_or_else(|| not_found(format!("id-index shard {}", shard_key)))?
        };

        // Step 6: Build a RangeChunkReader backed by our pre-fetched ranges,
        // then use the standard parquet reader to iterate the matching row group.
        let chunk_reader = RangeChunkReader {
            file_size,
            chunks: vec![(rg_start, rg_bytes), (tail_offset, tail_bytes)],
        };
        let file_reader = SerializedFileReader::new(chunk_reader)
            .map_err(|e| Error::RustError(format!("Failed to create reader: {}", e)))?;
        let rg_reader = file_reader
            .get_row_group(rg_idx)
            .map_err(|e| Error::RustError(format!("Failed to read row group: {}", e)))?;
        let iter = rg_reader
            .get_row_iter(None)
            .map_err(|e| Error::RustError(format!("Failed to iterate: {}", e)))?;
        for row in iter {
            let row = row.map_err(|e| Error::RustError(format!("Row read error: {}", e)))?;
            let id_bytes = row
                .get_bytes(0)
                .map_err(|e| Error::RustError(format!("Bad UUID column: {}", e)))?;
            if id_bytes.data() > target.as_slice() {
                return Ok(IdSearchResult {
                    result: None,
                    version: version.to_string(),
                });
            }
            if id_bytes.data() == target.as_slice() {
                let bbox_xmin = row
                    .get_float(1)
                    .map_err(|e| Error::RustError(format!("Bad bbox: {}", e)))?
                    as f64;
                let bbox_ymin = row
                    .get_float(2)
                    .map_err(|e| Error::RustError(format!("Bad bbox: {}", e)))?
                    as f64;
                let bbox_xmax = row
                    .get_float(3)
                    .map_err(|e| Error::RustError(format!("Bad bbox: {}", e)))?
                    as f64;
                let bbox_ymax = row
                    .get_float(4)
                    .map_err(|e| Error::RustError(format!("Bad bbox: {}", e)))?
                    as f64;
                let locator = if let Some((source_file_id, last_seen_release_id, registry_member)) =
                    compact_locator_ids(&row, id_config.format_version)
                {
                    match self.load_locator_dictionary(version, &id_config).await {
                        Ok(dictionary) => build_locator_metadata(
                            source_file_id,
                            last_seen_release_id,
                            registry_member,
                            &dictionary,
                        ),
                        Err(error) => {
                            console_log!(
                                "ID locator unavailable for {}: {:?}; returning legacy bbox",
                                gers_id,
                                error
                            );
                            None
                        }
                    }
                } else {
                    None
                };
                return Ok(IdSearchResult {
                    result: Some(IdLookupResult {
                        id: gers_id.to_string(),
                        bbox: geocoder_core::BBox {
                            xmin: bbox_xmin,
                            ymin: bbox_ymin,
                            xmax: bbox_xmax,
                            ymax: bbox_ymax,
                        },
                        locator,
                    }),
                    version: version.to_string(),
                });
            }
        }
        Ok(IdSearchResult {
            result: None,
            version: version.to_string(),
        })
    }

    /// Load the ID index prefix_len from a small metadata file.
    /// Falls back to id-collection.json summaries if id-meta.json doesn't exist.
    async fn load_id_index_config(&self, version: &str) -> Result<IdIndexConfig> {
        // Try tiny metadata file first (avoids loading multi-MB collection).
        // id-index TTL: patch runs re-upload these files in place.
        let meta_key = format!("{}/id-meta.json", version);
        if let Some(text) = self
            .memoized_get_text(&meta_key, ID_INDEX_CACHE_TTL)
            .await?
        {
            return parse_id_index_config(&text)
                .map_err(|e| not_found(format!("invalid id-index metadata {}: {}", meta_key, e)));
        }

        // Fallback: load id-collection.json. Its v3 fields live under
        // summaries; legacy collections produce a format-v1 config.
        let key = format!("{}/id-collection.json", version);
        if let Some(text) = self.memoized_get_text(&key, ID_INDEX_CACHE_TTL).await? {
            return parse_id_index_config(&text)
                .map_err(|e| not_found(format!("invalid id-index metadata {}: {}", key, e)));
        }

        // Both metadata files missing: the id-index isn't deployed for this
        // version. Surface a retriable not-found (so version fallback engages)
        // rather than guessing a shard layout and returning clean 404s.
        Err(not_found(format!(
            "id-index metadata for version {}",
            version
        )))
    }

    async fn load_locator_dictionary(
        &self,
        version: &str,
        config: &IdIndexConfig,
    ) -> Result<Rc<LocatorDictionary>> {
        let reference = config.locator_dictionary.as_ref().ok_or_else(|| {
            Error::RustError("ID-index format has no locator dictionary reference".into())
        })?;
        let expected_release = config
            .overture_release
            .as_deref()
            .ok_or_else(|| Error::RustError("ID-index format has no Overture release".into()))?;
        let href = reference.href.trim_start_matches("./");
        let key = format!("{}/{}", version, href);
        if let Some(cached) = LOCATOR_DICTIONARY_CACHE.with(|cache| {
            let mut cache = cache.borrow_mut();
            let position = cache
                .iter()
                .position(|(cached_key, _)| cached_key == &key)?;
            let entry = cache.remove(position);
            let result = Rc::clone(&entry.1);
            cache.push(entry);
            Some(result)
        }) {
            return Ok(cached);
        }
        let text = self
            .memoized_get_text(&key, IMMUTABLE_CACHE_TTL)
            .await?
            .ok_or_else(|| not_found(format!("ID locator dictionary {}", key)))?;
        if text.len() > 1024 * 1024 {
            return Err(Error::RustError(
                "ID locator dictionary exceeds 1 MiB".into(),
            ));
        }
        if text.len() != reference.size_bytes {
            return Err(Error::RustError(format!(
                "ID locator dictionary size mismatch for {}",
                key
            )));
        }
        let actual_sha256 = format!("{:x}", Sha256::digest(text.as_bytes()));
        if actual_sha256 != reference.sha256 {
            return Err(Error::RustError(format!(
                "ID locator dictionary checksum mismatch for {}",
                key
            )));
        }
        let dictionary: LocatorDictionary = serde_json::from_str(&text).map_err(|error| {
            Error::RustError(format!("Invalid ID locator dictionary {}: {}", key, error))
        })?;
        validate_locator_dictionary(&dictionary, reference, expected_release).map_err(|error| {
            Error::RustError(format!("Invalid ID locator dictionary {}: {}", key, error))
        })?;
        let dictionary = Rc::new(dictionary);
        LOCATOR_DICTIONARY_CACHE.with(|cache| {
            let mut cache = cache.borrow_mut();
            cache.push((key, Rc::clone(&dictionary)));
            if cache.len() > LOCATOR_DICTIONARY_CACHE_MAX_ENTRIES {
                cache.remove(0);
            }
        });
        Ok(dictionary)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use parquet::record::{Field, Row};

    const DICTIONARY_SHA: &str = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";

    fn dictionary_reference() -> LocatorDictionaryReference {
        LocatorDictionaryReference {
            href: format!("./id-locator-dictionary-{DICTIONARY_SHA}.json"),
            sha256: DICTIONARY_SHA.to_string(),
            size_bytes: 512,
            dictionary_version: 1,
            source_files_count: 1,
            last_seen_releases_count: 1,
            source_file_id_bounds: Some([1, 1]),
            last_seen_release_id_bounds: Some([1, 1]),
        }
    }

    fn locator_dictionary() -> LocatorDictionary {
        LocatorDictionary {
            format_version: 3,
            dictionary_version: 1,
            overture_release: "2026-06-17.0".to_string(),
            type_theme_map: TypeThemeMap {
                version: 1,
                types: HashMap::from([("address".to_string(), "addresses".to_string())]),
            },
            source_files: vec![SourceFileEntry {
                theme: "addresses".to_string(),
                feature_type: "address".to_string(),
                filename: "part-00001.zstd.parquet".to_string(),
            }],
            last_seen_releases: vec!["2026-05-20.0".to_string()],
            source_files_count: 1,
            last_seen_releases_count: 1,
            source_file_id_bounds: Some([1, 1]),
            last_seen_release_id_bounds: Some([1, 1]),
        }
    }

    fn locator_row(source_id: Field, release_id: Field, registry_member: bool) -> Row {
        let mut fields: Vec<_> = (0..5)
            .map(|index| (format!("bbox_{index}"), Field::Null))
            .collect();
        fields.extend([
            ("source_file_id".to_string(), source_id),
            ("last_seen_release_id".to_string(), release_id),
            ("registry_member".to_string(), Field::Bool(registry_member)),
        ]);
        Row::new(fields)
    }

    #[test]
    fn test_v1_id_metadata_stays_legacy() {
        let config =
            parse_id_index_config(r#"{"prefix_len":3,"overture_release":"2026-06-17.0"}"#).unwrap();
        assert_eq!(config.format_version, 1);
        assert!(config.overture_release.is_none());
        assert!(config.locator_dictionary.is_none());
    }

    #[test]
    fn test_v3_collection_and_dictionary_build_current_release_path() {
        let text = format!(
            r#"{{"summaries":{{"prefix_len":3,"format_version":3,
                "overture_release":"2026-06-17.0",
                "locator_dictionary":{{"href":"./id-locator-dictionary-{DICTIONARY_SHA}.json",
                "sha256":"{DICTIONARY_SHA}","size_bytes":512,"dictionary_version":1,
                "source_files_count":1,"last_seen_releases_count":1,
                "source_file_id_bounds":[1,1],"last_seen_release_id_bounds":[1,1]}}}}}}"#
        );
        let config = parse_id_index_config(&text).unwrap();
        assert!(config.locator_dictionary.is_some());
        let dictionary = locator_dictionary();
        validate_locator_dictionary(&dictionary, &dictionary_reference(), "2026-06-17.0").unwrap();
        let locator = build_locator_metadata(Some(1), None, false, &dictionary).unwrap();
        assert_eq!(locator.theme.as_deref(), Some("addresses"));
        assert!(locator.exists_in_current_release);
        assert_eq!(
            locator.overture_path.as_deref(),
            Some("release/2026-06-17.0/theme=addresses/type=address/part-00001.zstd.parquet")
        );
    }

    #[test]
    fn test_v3_historical_id_has_no_current_path() {
        let locator = build_locator_metadata(None, Some(1), true, &locator_dictionary()).unwrap();
        assert!(!locator.exists_in_current_release);
        assert!(locator.filename.is_none());
        assert!(locator.overture_path.is_none());
        assert_eq!(locator.last_seen_release.as_deref(), Some("2026-05-20.0"));
    }

    #[test]
    fn test_v3_invalid_or_out_of_range_ids_have_no_locator() {
        let dictionary = locator_dictionary();
        assert!(build_locator_metadata(None, None, true, &dictionary).is_none());
        assert!(build_locator_metadata(Some(2), None, true, &dictionary).is_none());
        assert!(build_locator_metadata(None, Some(2), true, &dictionary).is_none());
    }

    #[test]
    fn test_v3_metadata_requires_supported_complete_contract() {
        for text in [
            r#"{"prefix_len":3,"format_version":3}"#,
            r#"{"prefix_len":3,"format_version":"2"}"#,
            r#"{"prefix_len":3,"format_version":null}"#,
            r#"{"prefix_len":3,"format_version":2.0}"#,
            r#"{"prefix_len":3,"format_version":2}"#,
        ] {
            assert!(parse_id_index_config(text).is_err(), "accepted {text}");
        }
        let mut reference = dictionary_reference();
        reference.href = format!("./id-locator-dictionary-{}-extra.json", reference.sha256);
        assert!(validate_dictionary_reference(&reference).is_err());
    }

    #[test]
    fn test_v3_compact_row_validation_fails_closed() {
        let short = Row::new(
            (0..5)
                .map(|index| (format!("legacy_{index}"), parquet::record::Field::Null))
                .collect(),
        );
        assert!(compact_locator_ids(&short, 3).is_none());
        assert_eq!(
            compact_locator_ids(&locator_row(Field::Int(1), Field::Null, true), 3),
            Some((Some(1), None, true))
        );
        assert_eq!(
            compact_locator_ids(&locator_row(Field::Null, Field::Int(1), true), 3),
            Some((None, Some(1), true))
        );
        assert!(compact_locator_ids(&locator_row(Field::Int(0), Field::Null, true), 3).is_none());
        assert!(
            compact_locator_ids(&locator_row(Field::Int(-1), Field::Int(1), true), 3).is_none()
        );
        assert!(
            compact_locator_ids(&locator_row(Field::Int(1), Field::Int(70_000), true), 3).is_none()
        );
        assert!(compact_locator_ids(&locator_row(Field::Int(1), Field::Int(1), true), 3).is_none());
        assert!(compact_locator_ids(&locator_row(Field::Null, Field::Null, true), 3).is_none());
    }

    #[test]
    fn test_footer_retry_decision_covers_initial_and_exact_retry_paths() {
        assert_eq!(footer_retry_size(1_000_000, 32_768, 7_313), Ok(None));
        assert_eq!(
            footer_retry_size(1_000_000, 32_768, 39_992),
            Ok(Some(40_000))
        );
        // R2 returns the complete small file even though 32 KiB was requested.
        assert_eq!(footer_retry_size(10_000, 10_000, 8_992), Ok(None));
    }

    #[test]
    fn test_footer_retry_rejects_corrupt_or_implausible_lengths() {
        assert!(footer_retry_size(1_000, 1_000, 1_001).is_err());
        assert!(footer_retry_size(
            (MAX_PARQUET_FOOTER_SIZE + 9) as u64,
            32_768,
            MAX_PARQUET_FOOTER_SIZE + 1,
        )
        .is_err());
        assert!(footer_retry_size(u64::MAX, 32_768, usize::MAX).is_err());
        assert!(footer_retry_size(1_000, 1_001, 100).is_err());
    }

    #[test]
    fn test_footer_retry_rejects_mixed_object_generations() {
        fn footer(metadata_len: u32, magic: &[u8; 4]) -> Vec<u8> {
            let mut bytes = vec![0; metadata_len as usize];
            bytes.extend_from_slice(&metadata_len.to_le_bytes());
            bytes.extend_from_slice(magic);
            bytes
        }

        let expected = footer(40_000, b"PAR1");
        assert_eq!(
            validate_footer_retry_response(1_000_000, 40_000, 1_000_000, &expected),
            Ok(())
        );
        assert!(validate_footer_retry_response(1_000_000, 40_000, 1_000_001, &expected,).is_err());
        assert!(validate_footer_retry_response(
            1_000_000,
            40_000,
            1_000_000,
            &footer(39_999, b"PAR1"),
        )
        .is_err());
        assert!(validate_footer_retry_response(
            1_000_000,
            40_000,
            1_000_000,
            &footer(40_000, b"NOPE"),
        )
        .is_err());
    }

    #[test]
    fn test_parse_uuid_bytes_hyphenated() {
        let bytes = parse_uuid_bytes("08b2a100-d664-7fff-0200-a44bcea04b76").unwrap();
        assert_eq!(
            bytes,
            [
                0x08, 0xb2, 0xa1, 0x00, 0xd6, 0x64, 0x7f, 0xff, 0x02, 0x00, 0xa4, 0x4b, 0xce, 0xa0,
                0x4b, 0x76
            ]
        );
    }

    #[test]
    fn test_parse_uuid_bytes_plain() {
        let bytes = parse_uuid_bytes("08b2a100d6647fff0200a44bcea04b76").unwrap();
        assert_eq!(
            bytes,
            [
                0x08, 0xb2, 0xa1, 0x00, 0xd6, 0x64, 0x7f, 0xff, 0x02, 0x00, 0xa4, 0x4b, 0xce, 0xa0,
                0x4b, 0x76
            ]
        );
    }

    #[test]
    fn test_parse_uuid_bytes_invalid() {
        assert!(parse_uuid_bytes("too-short").is_none());
        assert!(parse_uuid_bytes("").is_none());
        assert!(parse_uuid_bytes("zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz").is_none());
    }
}
