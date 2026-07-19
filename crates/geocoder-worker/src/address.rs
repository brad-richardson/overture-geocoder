//! Structured address normalization, routing, and page lookup for `/v2/forward`.

use std::cell::RefCell;
use std::collections::{HashMap, HashSet};
use std::rc::Rc;

use serde::Deserialize;
use unicode_normalization::UnicodeNormalization;
use worker::*;

use crate::address_pages::AddressPageRecord;
use crate::stac::cache::IMMUTABLE_CACHE_TTL;
use crate::stac::ShardLoader;

/// The eight structured request fields, in the contract's normalized lookup
/// order. Positions 1 and 2 are the first and last retained source
/// `address_levels` values (`admin_level_general` / `admin_level_specific`).
pub(crate) const FIELD_NAMES: [&str; 8] = [
    "country",
    "admin_level_general",
    "admin_level_specific",
    "postal_city",
    "postcode",
    "street",
    "number",
    "unit",
];

/// Fields that must be non-empty after normalization (`country`, `street`,
/// `number`). Every other field may be empty, and an empty value is literal:
/// it matches producer rows whose normalized value is empty, not a wildcard.
const REQUIRED_NONEMPTY: [usize; 3] = [0, 5, 6];

/// Per-field raw byte cap, matching the smoke route's `k` bound. Rejects abusive
/// inputs before any normalization or R2 work.
const MAX_FIELD_BYTES: usize = 512;

/// Normalization contract version echoed to clients so a miss can be diagnosed.
/// Bumps when the NFC / Unicode-whitespace-collapse / ASCII-lowercase contract
/// changes (e.g. adopting broader Unicode case folding).
const NORMALIZATION_VERSION: &str = "nfc-uniws-asciilower-1";
const V2_NORMALIZATION_VERSION: &str = "nfc-uniws-collapse-ascii-lower-1";
const ADDRESS_PARTITION_SCHEME: &str = "country-fnv1a-high-bits-v1";
const MAX_HASH_PREFIX_BITS: usize = 24;
pub(crate) const MAX_ADDRESS_COLLECTION_BYTES: usize = 8 * 1024 * 1024;
const MAX_ADDRESS_COLLECTION_ROUTES: usize = 4_096;
const ADDRESS_COLLECTION_CACHE_MAX_ENTRIES: usize = 1;

thread_local! {
    /// Parsed immutable family routing manifests, LRU-last. The entry count is
    /// deliberately tiny: the live v2 catalog selects one source version, and
    /// roll-forward-only publication means an isolate needs only the current
    /// generation; an in-flight request keeps its own `Rc` alive during swap.
    static ADDRESS_COLLECTION_CACHE: RefCell<Vec<(String, Rc<PreparedAddressCollection>)>> =
        const { RefCell::new(Vec::new()) };
}

/// A request field failed contract validation.
#[derive(Debug)]
pub(crate) enum ValidationError {
    Missing(&'static str),
    Empty(&'static str),
    TooLong(&'static str),
}

impl ValidationError {
    pub(crate) fn message(&self) -> String {
        match self {
            Self::Missing(name) => format!("Missing required parameter: {name}"),
            Self::Empty(name) => format!("Parameter must not be empty: {name}"),
            Self::TooLong(name) => {
                format!("Parameter too long: {name} (max {MAX_FIELD_BYTES} bytes)")
            }
        }
    }
}

/// True for the whitespace set Python's `str.split()` collapses: the Unicode
/// `White_Space` property plus the C0 information separators `0x1C..=0x1F`,
/// which Python treats as whitespace but Rust's `char::is_whitespace` does not.
/// Matching this set exactly keeps the Worker's key byte-identical to the
/// producer's `" ".join(NFC(value).split())`.
fn is_key_whitespace(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

/// Reproduce the producer's structured-field normalization contract:
/// `" ".join(unicodedata.normalize("NFC", value).split()).translate(ASCII_LOWER)`.
///
/// 1. NFC normalization (canonically equivalent non-ASCII sequences merge).
/// 2. Unicode-whitespace collapse: split on runs of [`is_key_whitespace`] and
///    rejoin with a single ASCII space, dropping leading/trailing whitespace.
/// 3. ASCII-only lowercasing: only `A..=Z` fold to `a..=z`; non-ASCII case is
///    preserved (this slice does not promise Unicode case folding).
fn normalize_field(value: &str) -> String {
    let nfc: String = value.nfc().collect();
    let collapsed = nfc
        .split(is_key_whitespace)
        .filter(|segment| !segment.is_empty())
        .collect::<Vec<_>>()
        .join(" ");
    collapsed
        .chars()
        .map(|c| {
            if c.is_ascii_uppercase() {
                c.to_ascii_lowercase()
            } else {
                c
            }
        })
        .collect()
}

/// Validate the eight named params and build the normalized lookup key.
///
/// All eight keys must be present (an omitted field is a 400, never a wildcard).
/// `country`, `street`, and `number` must be non-empty after normalization.
pub(crate) fn build_lookup_key(
    params: &HashMap<String, String>,
) -> std::result::Result<[String; 8], ValidationError> {
    let mut normalized: [String; 8] = Default::default();
    for (index, name) in FIELD_NAMES.iter().enumerate() {
        let raw = params.get(*name).ok_or(ValidationError::Missing(name))?;
        if raw.len() > MAX_FIELD_BYTES {
            return Err(ValidationError::TooLong(name));
        }
        normalized[index] = normalize_field(raw);
    }
    for index in REQUIRED_NONEMPTY {
        if normalized[index].is_empty() {
            return Err(ValidationError::Empty(FIELD_NAMES[index]));
        }
    }
    Ok(normalized)
}

/// One immutable serving shard in the versioned address family manifest.
///
/// `index_href` / `data_href` are keys relative to the release version prefix.
/// `country` is the producer-normalized country code this shard serves. Optional
/// `[hash_start, hash_end]` is the inclusive routing range over the key hash;
/// see [`address_key_hash`].
#[derive(Debug, Deserialize)]
pub(crate) struct AddressShard {
    pub(crate) country: String,
    pub(crate) index_href: String,
    pub(crate) data_href: String,
    #[serde(default)]
    pub(crate) hash_start: Option<u64>,
    #[serde(default)]
    pub(crate) hash_end: Option<u64>,
    #[serde(default)]
    pub(crate) hash_prefix: Option<String>,
    #[serde(default)]
    pub(crate) hash_bits: Option<usize>,
    #[serde(default)]
    pub(crate) rows: Option<usize>,
    #[serde(default)]
    pub(crate) index_bytes: Option<usize>,
    #[serde(default)]
    pub(crate) index_sha256: Option<String>,
    #[serde(default)]
    pub(crate) data_bytes: Option<usize>,
    #[serde(default)]
    pub(crate) data_sha256: Option<String>,
}

#[derive(Debug, Deserialize)]
pub(crate) struct AddressEmptyRange {
    id: String,
    country: String,
    hash_prefix: String,
    hash_bits: usize,
    hash_start: u64,
    hash_end: u64,
    rows: usize,
}

impl AddressEmptyRange {
    fn contains_hash(&self, hash: u64) -> bool {
        self.hash_start <= hash && hash <= self.hash_end
    }
}

#[derive(Debug, Deserialize)]
pub(crate) struct AddressPartitionContract {
    scheme: String,
    maximum_hash_bits: usize,
    split_row_cap: usize,
    split_ids: Vec<String>,
}

impl AddressShard {
    fn contains_hash(&self, hash: u64) -> bool {
        match (self.hash_start, self.hash_end) {
            (Some(lo), Some(hi)) => lo <= hash && hash <= hi,
            // A country split across shards must declare explicit ranges;
            // otherwise it is not routable by hash.
            _ => false,
        }
    }
}

/// The `{version}/address-collection.json` family manifest.
///
/// Forward-compatible: unknown keys are ignored and every field defaults, so an
/// enriched producer manifest (lineage, tokenizer/format versions, richer
/// scope) still parses. `bbox` records the family's spatial scope for the
/// out-of-coverage hook; `items` maps shard id -> [`AddressShard`].
#[derive(Debug, Deserialize)]
pub(crate) struct AddressCollection {
    #[serde(default)]
    pub(crate) schema_version: u32,
    #[serde(default)]
    pub(crate) overture_release: Option<String>,
    #[serde(default)]
    #[allow(dead_code)]
    pub(crate) normalization_version: Option<String>,
    /// Family spatial scope `[min_lon, min_lat, max_lon, max_lat]`. Parsed and
    /// carried for the out-of-coverage hook below; finer spatial classification
    /// needs division enrichment that resolves a structured query to a region.
    #[serde(default)]
    #[allow(dead_code)]
    pub(crate) bbox: Option<[f64; 4]>,
    #[serde(default)]
    pub(crate) coverage: Option<[f64; 4]>,
    #[serde(default)]
    pub(crate) partition: Option<AddressPartitionContract>,
    #[serde(default)]
    pub(crate) items: HashMap<String, AddressShard>,
    #[serde(default)]
    pub(crate) empty_ranges: Vec<AddressEmptyRange>,
}

impl AddressCollection {
    fn response_normalization_version(&self) -> &'static str {
        if self.schema_version == 2 {
            V2_NORMALIZATION_VERSION
        } else {
            NORMALIZATION_VERSION
        }
    }

    fn supported(&self) -> bool {
        match self.schema_version {
            0 | 1 => self.supported_legacy(),
            2 => self.supported_v2(),
            _ => false,
        }
    }

    fn supported_legacy(&self) -> bool {
        self.partition.is_none()
            && self.coverage.is_none()
            && self.empty_ranges.is_empty()
            && !self.items.is_empty()
            && self.items.values().all(|shard| {
                valid_country(&shard.country)
                    && safe_relative_href(&shard.index_href)
                    && safe_relative_href(&shard.data_href)
                    && match (shard.hash_start, shard.hash_end) {
                        (None, None) => true,
                        (Some(lo), Some(hi)) => lo <= hi,
                        _ => false,
                    }
            })
    }

    fn supported_v2(&self) -> bool {
        let (Some(normalization), Some(release), Some(coverage), Some(contract)) = (
            self.normalization_version.as_deref(),
            self.overture_release.as_deref(),
            self.coverage,
            self.partition.as_ref(),
        ) else {
            return false;
        };
        if normalization != V2_NORMALIZATION_VERSION
            || release.is_empty()
            || coverage != [-180.0, -90.0, 180.0, 90.0]
            || self.bbox.is_some()
            || contract.scheme != ADDRESS_PARTITION_SCHEME
            || contract.maximum_hash_bits == 0
            || contract.maximum_hash_bits > MAX_HASH_PREFIX_BITS
            || contract.split_row_cap == 0
            || self
                .items
                .len()
                .checked_add(self.empty_ranges.len())
                .is_none_or(|routes| routes > MAX_ADDRESS_COLLECTION_ROUTES)
        {
            return false;
        }
        let Some(splits) = validated_split_ids(&contract.split_ids, contract.maximum_hash_bits)
        else {
            return false;
        };
        let mut countries: HashMap<&str, Vec<(u64, u64)>> = HashMap::new();
        let mut leaf_ids = HashSet::new();
        let mut used_splits = HashSet::new();
        for (id, shard) in &self.items {
            let (Some(prefix), Some(bits), Some(rows)) =
                (shard.hash_prefix.as_deref(), shard.hash_bits, shard.rows)
            else {
                return false;
            };
            if rows == 0
                || rows > contract.split_row_cap
                || !valid_leaf(
                    id,
                    &shard.country,
                    prefix,
                    bits,
                    shard.hash_start,
                    shard.hash_end,
                    &splits,
                    contract.maximum_hash_bits,
                )
                || shard.index_href != format!("families/addresses/shards/{id}.aidx")
                || shard.data_href != format!("families/addresses/shards/{id}.adat")
                || !positive_identity(shard.index_bytes, shard.index_sha256.as_deref())
                || !positive_identity(shard.data_bytes, shard.data_sha256.as_deref())
                || !leaf_ids.insert(id.as_str())
            {
                return false;
            }
            countries
                .entry(&shard.country)
                .or_default()
                .push((shard.hash_start.unwrap(), shard.hash_end.unwrap()));
            for length in 0..prefix.len() {
                used_splits.insert((shard.country.as_str(), &prefix[..length]));
            }
        }
        for empty in &self.empty_ranges {
            if empty.rows != 0
                || !valid_leaf(
                    &empty.id,
                    &empty.country,
                    &empty.hash_prefix,
                    empty.hash_bits,
                    Some(empty.hash_start),
                    Some(empty.hash_end),
                    &splits,
                    contract.maximum_hash_bits,
                )
                || !leaf_ids.insert(empty.id.as_str())
            {
                return false;
            }
            countries
                .entry(&empty.country)
                .or_default()
                .push((empty.hash_start, empty.hash_end));
            for length in 0..empty.hash_prefix.len() {
                used_splits.insert((empty.country.as_str(), &empty.hash_prefix[..length]));
            }
        }
        if countries.is_empty() || used_splits != splits {
            return false;
        }
        countries.values_mut().all(|ranges| {
            ranges.sort_unstable();
            let mut expected = 0_u128;
            for (start, end) in ranges {
                if u128::from(*start) != expected || start > end {
                    return false;
                }
                expected = u128::from(*end) + 1;
            }
            expected == 1_u128 << 64
        })
    }
}

enum AddressRouteTarget {
    Shard(String),
    Empty,
}

struct AddressRoute {
    hash_start: u64,
    hash_end: u64,
    target: AddressRouteTarget,
}

struct PreparedAddressCollection {
    collection: AddressCollection,
    routes: HashMap<String, Vec<AddressRoute>>,
}

impl PreparedAddressCollection {
    fn new(collection: AddressCollection) -> Self {
        let mut routes: HashMap<String, Vec<AddressRoute>> = HashMap::new();
        if collection.schema_version == 2 {
            for (id, shard) in &collection.items {
                routes
                    .entry(shard.country.clone())
                    .or_default()
                    .push(AddressRoute {
                        hash_start: shard.hash_start.expect("validated v2 shard start"),
                        hash_end: shard.hash_end.expect("validated v2 shard end"),
                        target: AddressRouteTarget::Shard(id.clone()),
                    });
            }
            for empty in &collection.empty_ranges {
                routes
                    .entry(empty.country.clone())
                    .or_default()
                    .push(AddressRoute {
                        hash_start: empty.hash_start,
                        hash_end: empty.hash_end,
                        target: AddressRouteTarget::Empty,
                    });
            }
            for country_routes in routes.values_mut() {
                country_routes.sort_unstable_by_key(|route| route.hash_start);
            }
        }
        Self { collection, routes }
    }

    fn select_shard<'a>(&'a self, key: &[String; 8]) -> ShardSelection<'a> {
        if self.collection.schema_version != 2 {
            return select_shard(&self.collection, key);
        }
        let Some(routes) = self.routes.get(key[0].as_str()) else {
            return ShardSelection::OutOfCoverage;
        };
        let hash = address_key_hash(key);
        let Some(index) = routes
            .partition_point(|route| route.hash_start <= hash)
            .checked_sub(1)
        else {
            return ShardSelection::Unroutable;
        };
        let route = &routes[index];
        if hash > route.hash_end {
            return ShardSelection::Unroutable;
        }
        match &route.target {
            AddressRouteTarget::Shard(id) => self
                .collection
                .items
                .get(id)
                .map_or(ShardSelection::Unroutable, ShardSelection::Shard),
            AddressRouteTarget::Empty => ShardSelection::Empty,
        }
    }
}

fn valid_country(country: &str) -> bool {
    (2..=3).contains(&country.len())
        && country
            .bytes()
            .all(|value| value.is_ascii_lowercase() || value.is_ascii_digit())
}

fn safe_relative_href(href: &str) -> bool {
    !href.is_empty()
        && !href.starts_with('/')
        && href.len() <= 256
        && href.split('/').all(|component| {
            !component.is_empty()
                && component != "."
                && component != ".."
                && component.bytes().all(|value| {
                    value.is_ascii_alphanumeric() || matches!(value, b'-' | b'_' | b'.')
                })
        })
}

fn valid_hash_prefix(prefix: &str, maximum: usize) -> bool {
    prefix.len() <= maximum && prefix.bytes().all(|value| matches!(value, b'0' | b'1'))
}

fn prefix_range(prefix: &str) -> Option<(u64, u64)> {
    if !valid_hash_prefix(prefix, MAX_HASH_PREFIX_BITS) {
        return None;
    }
    if prefix.is_empty() {
        return Some((0, u64::MAX));
    }
    let value = u64::from_str_radix(prefix, 2).ok()?;
    let remaining = 64 - prefix.len();
    let start = value << remaining;
    Some((start, start + ((1_u64 << remaining) - 1)))
}

fn route_id(country: &str, prefix: &str) -> String {
    if prefix.is_empty() {
        format!("a-{country}")
    } else {
        format!("a-{country}-h-{prefix}")
    }
}

fn parse_split_id(value: &str, maximum: usize) -> Option<(&str, &str)> {
    let (country, prefix) = value.split_once(':')?;
    if !valid_country(country) || !valid_hash_prefix(prefix, maximum) || prefix.len() >= maximum {
        return None;
    }
    Some((country, prefix))
}

fn validated_split_ids(values: &[String], maximum: usize) -> Option<HashSet<(&str, &str)>> {
    let mut result = HashSet::new();
    for value in values {
        let identity = parse_split_id(value, maximum)?;
        if !result.insert(identity) {
            return None;
        }
    }
    if result.iter().any(|(country, prefix)| {
        !prefix.is_empty() && !result.contains(&(*country, &prefix[..prefix.len() - 1]))
    }) {
        return None;
    }
    Some(result)
}

#[allow(clippy::too_many_arguments)]
fn valid_leaf(
    id: &str,
    country: &str,
    prefix: &str,
    bits: usize,
    start: Option<u64>,
    end: Option<u64>,
    splits: &HashSet<(&str, &str)>,
    maximum: usize,
) -> bool {
    let (Some(start), Some(end)) = (start, end) else {
        return false;
    };
    valid_country(country)
        && valid_hash_prefix(prefix, maximum)
        && bits == prefix.len()
        && Some((start, end)) == prefix_range(prefix)
        && id == route_id(country, prefix)
        && !splits.contains(&(country, prefix))
        && (prefix.is_empty() || splits.contains(&(country, &prefix[..prefix.len() - 1])))
}

fn positive_identity(bytes: Option<usize>, sha256: Option<&str>) -> bool {
    bytes.is_some_and(|value| value > 0)
        && sha256.is_some_and(|value| {
            value.len() == 64
                && value
                    .bytes()
                    .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        })
}

/// Stable 64-bit hash of the complete normalized eight-field key (FNV-1a over
/// the fields joined by `0x1f`). This is the Worker's routing contract: a
/// producer that splits a country across shards MUST partition by this exact
/// hash so every duplicate candidate for a key lands on one shard. Single-shard
/// countries (today's regional reality) never consult it.
fn address_key_hash(key: &[String; 8]) -> u64 {
    const OFFSET: u64 = 0xcbf2_9ce4_8422_2325;
    const PRIME: u64 = 0x0000_0100_0000_01b3;
    let mut hash = OFFSET;
    for (index, field) in key.iter().enumerate() {
        if index > 0 {
            hash ^= 0x1f;
            hash = hash.wrapping_mul(PRIME);
        }
        for byte in field.as_bytes() {
            hash ^= u64::from(*byte);
            hash = hash.wrapping_mul(PRIME);
        }
    }
    hash
}

/// Result of routing a normalized key to a serving shard.
enum ShardSelection<'a> {
    Shard(&'a AddressShard),
    /// The v2 collection proves this hash range contains no retained rows.
    Empty,
    /// The family serves no shard for the query's country.
    OutOfCoverage,
    /// The country is served but no shard's hash range covers the key (a
    /// malformed or incomplete manifest). Defensive; not expected in practice.
    Unroutable,
}

/// Route a normalized key to exactly one shard: filter by country, then, for a
/// country split across shards, select by [`address_key_hash`] range.
fn select_shard<'a>(collection: &'a AddressCollection, key: &[String; 8]) -> ShardSelection<'a> {
    let country = key[0].as_str();
    if collection.schema_version == 2 {
        let hash = address_key_hash(key);
        if let Some(shard) = collection
            .items
            .values()
            .find(|shard| shard.country == country && shard.contains_hash(hash))
        {
            return ShardSelection::Shard(shard);
        }
        if collection
            .empty_ranges
            .iter()
            .any(|range| range.country == country && range.contains_hash(hash))
        {
            return ShardSelection::Empty;
        }
        return if collection
            .items
            .values()
            .any(|shard| shard.country == country)
            || collection
                .empty_ranges
                .iter()
                .any(|range| range.country == country)
        {
            ShardSelection::Unroutable
        } else {
            ShardSelection::OutOfCoverage
        };
    }
    let matching: Vec<&AddressShard> = collection
        .items
        .values()
        .filter(|shard| shard.country == country)
        .collect();
    match matching.as_slice() {
        [] => ShardSelection::OutOfCoverage,
        [single] => ShardSelection::Shard(single),
        many => {
            let hash = address_key_hash(key);
            many.iter()
                .copied()
                .find(|shard| shard.contains_hash(hash))
                .map_or(ShardSelection::Unroutable, ShardSelection::Shard)
        }
    }
}

/// Outcome of an address lookup, before HTTP shaping.
pub(crate) enum AddressOutcome {
    /// The family provably does not serve the query's country.
    OutOfCoverage {
        data_version: String,
        normalization_version: &'static str,
    },
    /// The family served the query; `candidates` is every exact-key match in
    /// producer order (possibly empty for a successful exact miss).
    Resolved {
        data_version: String,
        normalization_version: &'static str,
        candidates: Vec<AddressPageRecord>,
    },
}

impl ShardLoader {
    async fn load_address_collection_key(
        &self,
        key: &str,
    ) -> Result<Option<Rc<PreparedAddressCollection>>> {
        let cached = ADDRESS_COLLECTION_CACHE.with(|cache| {
            let mut cache = cache.borrow_mut();
            cache
                .iter()
                .position(|(cached_key, _)| cached_key == key)
                .map(|position| {
                    let entry = cache.remove(position);
                    let collection = Rc::clone(&entry.1);
                    cache.push(entry);
                    collection
                })
        });
        if let Some(collection) = cached {
            return Ok(Some(collection));
        }

        let Some(text) = self
            .memoized_get_bounded_text(key, MAX_ADDRESS_COLLECTION_BYTES, IMMUTABLE_CACHE_TTL)
            .await?
        else {
            return Ok(None);
        };
        self.forget_memoized_text(key);
        let collection: AddressCollection = serde_json::from_str(&text)
            .map_err(|e| Error::RustError(format!("Invalid {key}: {e}")))?;
        if !collection.supported() {
            return Err(Error::RustError(format!(
                "Unsupported address collection contract: {key}"
            )));
        }
        let collection = Rc::new(PreparedAddressCollection::new(collection));
        ADDRESS_COLLECTION_CACHE.with(|cache| {
            let mut cache = cache.borrow_mut();
            if !cache.iter().any(|(cached_key, _)| cached_key == key) {
                cache.push((key.to_string(), Rc::clone(&collection)));
                while cache.len() > ADDRESS_COLLECTION_CACHE_MAX_ENTRIES {
                    cache.remove(0);
                }
            }
        });
        Ok(Some(collection))
    }

    /// Resolve an exact structured lookup through the collection object named
    /// by an atomic v2 release manifest. Shard hrefs are relative to the
    /// independent family source version, not to the geocoder build identity.
    pub(crate) async fn lookup_address_entrypoint(
        &self,
        key: &[String; 8],
        collection_key: &str,
        geocoder_build: &str,
    ) -> Result<AddressOutcome> {
        const SUFFIX: &str = "/families/addresses/address-collection.json";
        let data_root = collection_key.strip_suffix(SUFFIX).ok_or_else(|| {
            Error::RustError("v2 address entrypoint is outside its canonical family path".into())
        })?;
        if data_root.is_empty() || data_root.contains('/') {
            return Err(Error::RustError(
                "v2 address entrypoint has an invalid source version".into(),
            ));
        }
        let collection = self
            .load_address_collection_key(collection_key)
            .await?
            .ok_or_else(|| crate::stac::not_found(collection_key))?;
        self.lookup_address_collection(
            key,
            data_root,
            geocoder_build.to_string(),
            collection.as_ref(),
        )
        .await
    }

    async fn lookup_address_collection(
        &self,
        key: &[String; 8],
        data_root: &str,
        data_version: String,
        collection: &PreparedAddressCollection,
    ) -> Result<AddressOutcome> {
        let normalization_version = collection.collection.response_normalization_version();
        match collection.select_shard(key) {
            ShardSelection::OutOfCoverage => Ok(AddressOutcome::OutOfCoverage {
                data_version,
                normalization_version,
            }),
            ShardSelection::Unroutable => Err(Error::RustError(format!(
                "address family manifest for {data_version} has no shard range covering the key"
            ))),
            ShardSelection::Empty => Ok(AddressOutcome::Resolved {
                data_version,
                normalization_version,
                candidates: Vec::new(),
            }),
            ShardSelection::Shard(shard) => {
                let index_key = format!("{data_root}/{}", shard.index_href);
                let data_key = format!("{data_root}/{}", shard.data_href);
                let lookup = self.lookup_address_page(&index_key, &data_key, key).await?;
                Ok(AddressOutcome::Resolved {
                    data_version,
                    normalization_version,
                    candidates: lookup.records,
                })
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn params(pairs: &[(&str, &str)]) -> HashMap<String, String> {
        pairs
            .iter()
            .map(|(k, v)| ((*k).to_string(), (*v).to_string()))
            .collect()
    }

    fn full_params() -> Vec<(&'static str, &'static str)> {
        vec![
            ("country", "US"),
            ("admin_level_general", "MA"),
            ("admin_level_specific", "Middlesex"),
            ("postal_city", "Stoneham"),
            ("postcode", "02180"),
            ("street", "Main Street"),
            ("number", "10"),
            ("unit", ""),
        ]
    }

    fn shard(country: &str, hash: Option<(u64, u64)>) -> AddressShard {
        AddressShard {
            country: country.to_string(),
            index_href: format!("address/{country}.idx"),
            data_href: format!("address/{country}.bin"),
            hash_start: hash.map(|(lo, _)| lo),
            hash_end: hash.map(|(_, hi)| hi),
            hash_prefix: None,
            hash_bits: None,
            rows: None,
            index_bytes: None,
            index_sha256: None,
            data_bytes: None,
            data_sha256: None,
        }
    }

    fn legacy_collection(items: HashMap<String, AddressShard>) -> AddressCollection {
        AddressCollection {
            schema_version: 0,
            overture_release: None,
            normalization_version: None,
            bbox: None,
            coverage: None,
            partition: None,
            items,
            empty_ranges: Vec::new(),
        }
    }

    // --- Normalization (pinned against the producer's Python `normalize`) ---

    #[test]
    fn normalizes_exactly_like_the_producer() {
        assert_eq!(normalize_field("  Main   Street  "), "main street");
        // ASCII-only lowercasing leaves non-ASCII uppercase intact.
        assert_eq!(normalize_field("MÜNCHEN"), "mÜnchen");
        assert_eq!(normalize_field("Café"), "café");
        assert_eq!(normalize_field("US"), "us");
        assert_eq!(normalize_field("10-A"), "10-a");
        // No Unicode case folding: ß is preserved.
        assert_eq!(normalize_field("STRASSEß"), "strasseß");
        assert_eq!(normalize_field("  "), "");
    }

    #[test]
    fn collapses_nbsp_and_c0_separators_like_python_split() {
        // NBSP (U+00A0) is Unicode White_Space.
        assert_eq!(normalize_field("\u{a0}NBSP\u{a0}here"), "nbsp here");
        // C0 separators 0x1C/0x1D are whitespace to Python's split() but not to
        // Rust's char::is_whitespace; is_key_whitespace bridges the gap.
        assert_eq!(normalize_field("A\u{1c}B\u{1d}C"), "a b c");
    }

    #[test]
    fn composes_decomposed_sequences_before_lookup() {
        // "Cafe" + combining acute accent composes to precomposed "café".
        assert_eq!(normalize_field("Cafe\u{301}"), "café");
    }

    /// Cross-language normalization parity. Each expectation is the byte-exact
    /// output of the producer's Python
    /// `" ".join(unicodedata.normalize("NFC", v).split()).translate(ASCII_LOWER)`
    /// on the same input (generated with explicit code points so both sides are
    /// unambiguous). A future producer or a Unicode-data bump in either language
    /// that diverges on these adversarial vectors trips this test rather than
    /// silently routing address lookups to the wrong key.
    #[test]
    fn matches_producer_normalize_on_adversarial_vectors() {
        // Unicode-whitespace collapse (each is White_Space or a C0 separator).
        assert_eq!(normalize_field("A\u{3000}B"), "a b"); // ideographic space
        assert_eq!(normalize_field("A\u{85}B"), "a b"); // NEL
        assert_eq!(normalize_field("  A\u{9}\u{a0}\u{3000} B  "), "a b"); // mixed run
                                                                          // Turkish dotted/dotless I: ASCII-only lowercasing must NOT touch them,
                                                                          // and NFC must not decompose U+0130 into "I + combining dot".
        assert_eq!(normalize_field("\u{130}STANBUL"), "\u{130}stanbul");
        assert_eq!(normalize_field("\u{131}RMAK"), "\u{131}rmak");
        // NFC (not NFKC): full-width forms are preserved, not folded to ASCII.
        assert_eq!(
            normalize_field("\u{ff11}\u{ff12}\u{ff13}"),
            "\u{ff11}\u{ff12}\u{ff13}"
        );
        assert_eq!(
            normalize_field("\u{ff21}\u{ff22}\u{ff23}"),
            "\u{ff21}\u{ff22}\u{ff23}"
        );
        // Canonical singletons recompose identically to Python's NFC.
        assert_eq!(normalize_field("\u{212b}NGSTROM"), "\u{c5}ngstrom"); // Å sign -> U+00C5
        assert_eq!(normalize_field("\u{2126}"), "\u{3a9}"); // Ohm sign -> U+03A9
                                                            // Combining sequence composes; zero-width space is not whitespace.
        assert_eq!(normalize_field("Cafe\u{301}"), "caf\u{e9}");
        assert_eq!(normalize_field("A\u{200b}B"), "a\u{200b}b");
    }

    // --- Validation ---

    #[test]
    fn builds_key_in_contract_order() {
        let key = build_lookup_key(&params(&full_params())).unwrap();
        assert_eq!(
            key,
            [
                "us",
                "ma",
                "middlesex",
                "stoneham",
                "02180",
                "main street",
                "10",
                ""
            ]
            .map(str::to_string)
        );
    }

    #[test]
    fn rejects_a_missing_field() {
        let mut pairs = full_params();
        pairs.retain(|(name, _)| *name != "unit");
        let error = build_lookup_key(&params(&pairs)).unwrap_err();
        assert!(matches!(error, ValidationError::Missing("unit")));
    }

    #[test]
    fn empty_unit_is_a_literal_value_not_a_wildcard() {
        // Empty non-required fields are allowed and normalize to "".
        let key = build_lookup_key(&params(&full_params())).unwrap();
        assert_eq!(key[7], "");
    }

    #[test]
    fn rejects_empty_required_fields() {
        for field in ["country", "street", "number"] {
            let mut pairs = full_params();
            for pair in &mut pairs {
                if pair.0 == field {
                    pair.1 = "   ";
                }
            }
            let error = build_lookup_key(&params(&pairs)).unwrap_err();
            assert!(matches!(error, ValidationError::Empty(name) if name == field));
        }
    }

    #[test]
    fn rejects_overlong_fields() {
        let long = "a".repeat(MAX_FIELD_BYTES + 1);
        let mut pairs = full_params();
        for pair in &mut pairs {
            if pair.0 == "street" {
                pair.1 = &long;
            }
        }
        let error = build_lookup_key(&params(&pairs)).unwrap_err();
        assert!(matches!(error, ValidationError::TooLong("street")));
    }

    // --- Shard routing ---

    #[test]
    fn routes_single_country_shard_without_hashing() {
        let mut items = HashMap::new();
        items.insert("us-0".to_string(), shard("us", None));
        let collection = legacy_collection(items);
        let key = build_lookup_key(&params(&full_params())).unwrap();
        assert!(matches!(
            select_shard(&collection, &key),
            ShardSelection::Shard(shard) if shard.country == "us"
        ));
    }

    #[test]
    fn reports_out_of_coverage_for_an_unserved_country() {
        let mut items = HashMap::new();
        items.insert("us-0".to_string(), shard("us", None));
        let collection = legacy_collection(items);
        let mut pairs = full_params();
        for pair in &mut pairs {
            if pair.0 == "country" {
                pair.1 = "FR";
            }
        }
        let key = build_lookup_key(&params(&pairs)).unwrap();
        assert!(matches!(
            select_shard(&collection, &key),
            ShardSelection::OutOfCoverage
        ));
    }

    #[test]
    fn routes_a_split_country_by_key_hash() {
        let key = build_lookup_key(&params(&full_params())).unwrap();
        let hash = address_key_hash(&key);
        let mut items = HashMap::new();
        // One shard covers [0, hash], the other (hash+1, MAX]; the key must land
        // in the first.
        items.insert("us-lo".to_string(), shard("us", Some((0, hash))));
        items.insert(
            "us-hi".to_string(),
            shard("us", Some((hash.wrapping_add(1), u64::MAX))),
        );
        let collection = legacy_collection(items);
        match select_shard(&collection, &key) {
            ShardSelection::Shard(shard) => {
                assert_eq!(shard.hash_start, Some(0));
                assert_eq!(shard.hash_end, Some(hash));
            }
            _ => panic!("expected a hashed shard selection"),
        }
    }

    #[test]
    fn split_country_without_covering_range_is_unroutable() {
        let mut items = HashMap::new();
        items.insert("us-a".to_string(), shard("us", Some((0, 0))));
        items.insert("us-b".to_string(), shard("us", Some((1, 1))));
        let collection = legacy_collection(items);
        let key = build_lookup_key(&params(&full_params())).unwrap();
        assert!(matches!(
            select_shard(&collection, &key),
            ShardSelection::Unroutable
        ));
    }

    /// Pin the routing hash + key serialization as a shared cross-language
    /// vector. A producer that splits a country across shards MUST partition by
    /// this exact FNV-1a-over-`0x1f`-joined-fields hash; a mismatch would route
    /// duplicate candidates to the wrong shard and surface as a silent
    /// not-found. These constants are reproduced by the reference Python
    /// (`FNV-1a`, offset 0xcbf29ce484222325, prime 0x100000001b3, fields joined
    /// by a 0x1f separator byte) so both implementations can check against them.
    #[test]
    fn address_key_hash_is_a_pinned_cross_language_vector() {
        let key = build_lookup_key(&params(&full_params())).unwrap();
        assert_eq!(key[0], "us"); // guards the serialized byte stream below
        assert_eq!(address_key_hash(&key), 0x0ce4_f784_42ca_30b4);
        let key2 = ["fr", "", "", "", "", "rue de la paix", "1", ""].map(str::to_string);
        assert_eq!(address_key_hash(&key2), 0x20fd_8a67_97be_2b2b);
    }

    fn v2_collection() -> AddressCollection {
        let mut items = HashMap::new();
        items.insert(
            "a-us-h-0".into(),
            AddressShard {
                country: "us".into(),
                index_href: "families/addresses/shards/a-us-h-0.aidx".into(),
                data_href: "families/addresses/shards/a-us-h-0.adat".into(),
                hash_start: Some(0),
                hash_end: Some((1_u64 << 63) - 1),
                hash_prefix: Some("0".into()),
                hash_bits: Some(1),
                rows: Some(10),
                index_bytes: Some(100),
                index_sha256: Some("a".repeat(64)),
                data_bytes: Some(200),
                data_sha256: Some("b".repeat(64)),
            },
        );
        AddressCollection {
            schema_version: 2,
            overture_release: Some("2026-06-17.0".into()),
            normalization_version: Some(V2_NORMALIZATION_VERSION.into()),
            bbox: None,
            coverage: Some([-180.0, -90.0, 180.0, 90.0]),
            partition: Some(AddressPartitionContract {
                scheme: ADDRESS_PARTITION_SCHEME.into(),
                maximum_hash_bits: 16,
                split_row_cap: 1_000_000,
                split_ids: vec!["us:".into()],
            }),
            items,
            empty_ranges: vec![AddressEmptyRange {
                id: "a-us-h-1".into(),
                country: "us".into(),
                hash_prefix: "1".into(),
                hash_bits: 1,
                hash_start: 1_u64 << 63,
                hash_end: u64::MAX,
                rows: 0,
            }],
        }
    }

    #[test]
    fn v2_collection_routes_artifact_and_proven_empty_ranges() {
        let collection = v2_collection();
        assert!(collection.supported());
        let key = build_lookup_key(&params(&full_params())).unwrap();
        assert_eq!(address_key_hash(&key) >> 63, 0);
        assert!(matches!(
            select_shard(&collection, &key),
            ShardSelection::Shard(shard) if shard.hash_prefix.as_deref() == Some("0")
        ));

        let mut empty_key = key.clone();
        for number in 0..10_000 {
            empty_key[6] = number.to_string();
            if address_key_hash(&empty_key) >> 63 == 1 {
                break;
            }
        }
        assert_eq!(address_key_hash(&empty_key) >> 63, 1);
        assert!(matches!(
            select_shard(&collection, &empty_key),
            ShardSelection::Empty
        ));

        let prepared = PreparedAddressCollection::new(collection);
        assert!(matches!(
            prepared.select_shard(&key),
            ShardSelection::Shard(shard) if shard.hash_prefix.as_deref() == Some("0")
        ));
        assert!(matches!(
            prepared.select_shard(&empty_key),
            ShardSelection::Empty
        ));
    }

    #[test]
    fn v2_collection_rejects_missing_split_ancestry_and_unsafe_hrefs() {
        let mut missing_split = v2_collection();
        missing_split.partition.as_mut().unwrap().split_ids.clear();
        assert!(!missing_split.supported());

        let mut unsafe_href = v2_collection();
        unsafe_href.items.get_mut("a-us-h-0").unwrap().index_href = "../a-us-h-0.aidx".into();
        assert!(!unsafe_href.supported());

        let mut unused_split = v2_collection();
        unused_split
            .partition
            .as_mut()
            .unwrap()
            .split_ids
            .push("ca:".into());
        assert!(!unused_split.supported());
    }

    // --- Manifest deserialization ---

    #[test]
    fn parses_manifest_and_ignores_unknown_keys() {
        let json = r#"{
            "normalization_version": "nfc-uniws-asciilower-1",
            "bbox": [-80.5, 38.0, -66.9, 47.5],
            "lineage": {"ignored": true},
            "items": {
                "us-ne-0": {
                    "country": "us",
                    "index_href": "address/us-ne-0.idx",
                    "data_href": "address/us-ne-0.bin",
                    "hash_start": 0,
                    "hash_end": 100,
                    "extra": "ignored"
                }
            }
        }"#;
        let collection: AddressCollection = serde_json::from_str(json).unwrap();
        assert_eq!(
            collection.normalization_version.as_deref(),
            Some("nfc-uniws-asciilower-1")
        );
        assert_eq!(collection.bbox, Some([-80.5, 38.0, -66.9, 47.5]));
        let shard = &collection.items["us-ne-0"];
        assert_eq!(shard.country, "us");
        assert_eq!(shard.index_href, "address/us-ne-0.idx");
        assert!(shard.contains_hash(50));
        assert!(!shard.contains_hash(101));
    }
}
