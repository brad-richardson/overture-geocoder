//! Production structured `/address` exact-lookup route.
//!
//! This is the additive, always-compiled counterpart to the isolated
//! `/__address-page-spike` smoke route. It accepts the eight structured fields
//! from the address contract (`docs/address-structured-endpoint-contract.md`),
//! normalizes them exactly as the producer does, discovers the address family
//! from the release catalog, and reads candidates through the shared address
//! page reader ([`ShardLoader::lookup_address_page`]).
//!
//! The route is safe to ship before any address family is published: when the
//! catalog carries no `{version}/address-collection.json`, it returns a stable
//! `address_family_unavailable` 404 without touching the address read path.
//! It shares no state with, and does not change the behavior of, the existing
//! `/search`, `/reverse`, `/id`, or `/health` routes.

use std::collections::HashMap;

use serde::Deserialize;
use serde_json::{json, Value};
use unicode_normalization::UnicodeNormalization;
use worker::*;

use crate::address_pages::AddressPageRecord;
use crate::range_reader::RangeReadMetrics;
use crate::stac::cache::IMMUTABLE_CACHE_TTL;
use crate::stac::ShardLoader;

/// The eight structured request fields, in the contract's normalized lookup
/// order. Positions 1 and 2 are the first and last retained source
/// `address_levels` values (`admin_level_general` / `admin_level_specific`).
const FIELD_NAMES: [&str; 8] = [
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

/// Hard response candidate cap from the contract. Above the measured maximum
/// exact-key fanout of 252. Exceeding it is a bounded error, never a silent
/// truncation.
const CANDIDATE_CAP: usize = 512;

/// Normalization contract version echoed to clients so a miss can be diagnosed.
/// Bumps when the NFC / Unicode-whitespace-collapse / ASCII-lowercase contract
/// changes (e.g. adopting broader Unicode case folding).
const NORMALIZATION_VERSION: &str = "nfc-uniws-asciilower-1";

/// A request field failed contract validation.
#[derive(Debug)]
enum ValidationError {
    Missing(&'static str),
    Empty(&'static str),
    TooLong(&'static str),
}

impl ValidationError {
    fn message(&self) -> String {
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
fn build_lookup_key(
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
    #[allow(dead_code)]
    pub(crate) normalization_version: Option<String>,
    /// Family spatial scope `[min_lon, min_lat, max_lon, max_lat]`. Parsed and
    /// carried for the out-of-coverage hook below; finer spatial classification
    /// needs division enrichment that resolves a structured query to a region.
    #[serde(default)]
    #[allow(dead_code)]
    pub(crate) bbox: Option<[f64; 4]>,
    #[serde(default)]
    pub(crate) items: HashMap<String, AddressShard>,
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
enum AddressOutcome {
    /// No address family in the catalog -> stable 404.
    FamilyUnavailable,
    /// The family provably does not serve the query's country.
    OutOfCoverage { data_version: String },
    /// The family served the query; `candidates` is every exact-key match in
    /// producer order (possibly empty for a successful exact miss).
    Resolved {
        data_version: String,
        candidates: Vec<AddressPageRecord>,
        read_metrics: RangeReadMetrics,
    },
}

impl ShardLoader {
    /// Fetch the latest release's address family manifest, or `None` when the
    /// family is not published (a cheap, negative-cacheable catalog probe).
    async fn load_address_collection(&self) -> Result<Option<(String, AddressCollection)>> {
        let Some(version) = self.latest_version().await? else {
            return Err(Error::RustError("No versions found in catalog".into()));
        };
        let key = format!("{version}/address-collection.json");
        let Some(text) = self.memoized_get_text(&key, IMMUTABLE_CACHE_TTL).await? else {
            return Ok(None);
        };
        let collection: AddressCollection = serde_json::from_str(&text)
            .map_err(|e| Error::RustError(format!("Invalid {key}: {e}")))?;
        Ok(Some((version, collection)))
    }

    /// Resolve a normalized eight-field key to its address candidates.
    async fn lookup_address(&self, key: &[String; 8]) -> Result<AddressOutcome> {
        let Some((version, collection)) = self.load_address_collection().await? else {
            return Ok(AddressOutcome::FamilyUnavailable);
        };
        match select_shard(&collection, key) {
            ShardSelection::OutOfCoverage => Ok(AddressOutcome::OutOfCoverage {
                data_version: version,
            }),
            ShardSelection::Unroutable => Err(Error::RustError(format!(
                "address family manifest for {version} has no shard range covering the key"
            ))),
            ShardSelection::Shard(shard) => {
                let index_key = format!("{version}/{}", shard.index_href);
                let data_key = format!("{version}/{}", shard.data_href);
                let lookup = self.lookup_address_page(&index_key, &data_key, key).await?;
                Ok(AddressOutcome::Resolved {
                    data_version: version,
                    candidates: lookup.records,
                    read_metrics: lookup.read_metrics,
                })
            }
        }
    }
}

/// Stable machine-readable family-unavailable body.
fn family_unavailable_body() -> Value {
    json!({ "error": "address_family_unavailable" })
}

/// Out-of-coverage body: an explicit, non-error signal distinct from an exact
/// miss (`coverage: "in_coverage"`, empty candidates).
fn out_of_coverage_body(data_version: &str) -> Value {
    json!({
        "candidates": [],
        "candidate_count": 0,
        "ambiguous": false,
        "overflow": false,
        "coverage": "out_of_coverage",
        "data_version": data_version,
        "normalization_version": NORMALIZATION_VERSION,
    })
}

fn candidate_json(record: &AddressPageRecord) -> Value {
    json!({
        "id": record.id,
        "longitude": record.longitude,
        "latitude": record.latitude,
        "country": record.country,
        "postal_city": record.postal_city,
        "postcode": record.postcode,
        "street": record.street,
        "number": record.number,
        "unit": record.unit,
        "address_levels": record.address_levels,
        "source": {
            "object_index": record.source_object_index,
            "row_group": record.source_row_group,
            "row_index": record.source_row_index,
        },
    })
}

/// Shape resolved candidates into `(status, body)`. Every exact-key match is
/// returned in producer order with no dedup. Over the candidate cap is a bounded
/// 413 error carrying the observed count, never a truncation.
fn resolved_body(
    data_version: &str,
    candidates: &[AddressPageRecord],
    debug_metrics: Option<&RangeReadMetrics>,
) -> (u16, Value) {
    if candidates.len() > CANDIDATE_CAP {
        return (
            413,
            json!({
                "error": "address_candidate_overflow",
                "observed_candidates": candidates.len(),
                "candidate_cap": CANDIDATE_CAP,
                "overflow": true,
                "data_version": data_version,
                "normalization_version": NORMALIZATION_VERSION,
            }),
        );
    }
    let items: Vec<Value> = candidates.iter().map(candidate_json).collect();
    let mut body = json!({
        "candidates": items,
        "candidate_count": candidates.len(),
        "ambiguous": candidates.len() > 1,
        "overflow": false,
        "coverage": "in_coverage",
        "data_version": data_version,
        "normalization_version": NORMALIZATION_VERSION,
    });
    if let Some(metrics) = debug_metrics {
        if let Some(object) = body.as_object_mut() {
            object.insert("debug".to_string(), json!({ "read_metrics": metrics }));
        }
    }
    (200, body)
}

/// Map a lookup outcome to `(status, X-Data-Version, body)`. Pure so the route
/// contract (status codes, coverage distinction, version echo) is unit-tested
/// without a Worker environment.
fn shape_outcome(outcome: AddressOutcome, include_debug: bool) -> (u16, Option<String>, Value) {
    match outcome {
        AddressOutcome::FamilyUnavailable => (404, None, family_unavailable_body()),
        AddressOutcome::OutOfCoverage { data_version } => {
            let body = out_of_coverage_body(&data_version);
            (200, Some(data_version), body)
        }
        AddressOutcome::Resolved {
            data_version,
            candidates,
            read_metrics,
        } => {
            let (status, body) = resolved_body(
                &data_version,
                &candidates,
                include_debug.then_some(&read_metrics),
            );
            (status, Some(data_version), body)
        }
    }
}

/// Structured exact-address lookup handler.
pub async fn handle_address(
    req: Request,
    ctx: RouteContext<std::rc::Rc<Context>>,
) -> Result<Response> {
    let url = req.url()?;
    let params: HashMap<String, String> = url
        .query_pairs()
        .map(|(k, v)| (k.to_string(), v.to_string()))
        .collect();
    let include_debug = params
        .get("debug")
        .map(|d| d == "1" || d == "true")
        .unwrap_or(false);

    let key = match build_lookup_key(&params) {
        Ok(key) => key,
        Err(error) => return Response::error(error.message(), 400),
    };

    let loader = ShardLoader::with_context(&ctx.env, ctx.data.clone())?;
    let outcome = loader.lookup_address(&key).await?;
    let (status, data_version, body) = shape_outcome(outcome, include_debug);

    let mut response = Response::from_json(&body)?.with_status(status);
    response
        .headers_mut()
        .set("Content-Type", "application/json; charset=utf-8")?;
    if let Some(version) = data_version {
        response.headers_mut().set("X-Data-Version", &version)?;
    }
    Ok(response)
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

    fn record(id: &str, number: &str) -> AddressPageRecord {
        AddressPageRecord {
            key: [
                "us",
                "ma",
                "middlesex",
                "stoneham",
                "02180",
                "main street",
                number,
                "",
            ]
            .map(str::to_string),
            id: id.to_string(),
            longitude: -71.0999,
            latitude: 42.4801,
            source_object_index: 0,
            source_row_group: 12,
            source_row_index: 5,
            country: "US".to_string(),
            postal_city: "Stoneham".to_string(),
            postcode: "02180".to_string(),
            street: "Main Street".to_string(),
            number: number.to_string(),
            unit: String::new(),
            address_levels: vec!["MA".to_string(), "Middlesex".to_string()],
        }
    }

    fn shard(country: &str, hash: Option<(u64, u64)>) -> AddressShard {
        AddressShard {
            country: country.to_string(),
            index_href: format!("address/{country}.idx"),
            data_href: format!("address/{country}.bin"),
            hash_start: hash.map(|(lo, _)| lo),
            hash_end: hash.map(|(_, hi)| hi),
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
        let collection = AddressCollection {
            normalization_version: None,
            bbox: None,
            items,
        };
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
        let collection = AddressCollection {
            normalization_version: None,
            bbox: None,
            items,
        };
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
        let collection = AddressCollection {
            normalization_version: None,
            bbox: None,
            items,
        };
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
        let collection = AddressCollection {
            normalization_version: None,
            bbox: None,
            items,
        };
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

    // --- Response shaping ---

    #[test]
    fn family_unavailable_body_is_stable() {
        assert_eq!(
            family_unavailable_body(),
            json!({ "error": "address_family_unavailable" })
        );
    }

    #[test]
    fn out_of_coverage_is_distinct_from_an_exact_miss() {
        let out = out_of_coverage_body("2026-07-13.0");
        assert_eq!(out["coverage"], "out_of_coverage");
        assert_eq!(out["candidate_count"], 0);

        let (status, miss) = resolved_body("2026-07-13.0", &[], None);
        assert_eq!(status, 200);
        assert_eq!(miss["coverage"], "in_coverage");
        assert_eq!(miss["candidate_count"], 0);
        assert_eq!(miss["ambiguous"], false);
    }

    #[test]
    fn returns_all_duplicate_candidates_in_order_without_dedup() {
        let candidates = vec![
            record("id-a", "10"),
            record("id-b", "10"),
            record("id-c", "10"),
        ];
        let (status, body) = resolved_body("2026-07-13.0", &candidates, None);
        assert_eq!(status, 200);
        assert_eq!(body["candidate_count"], 3);
        assert_eq!(body["ambiguous"], true);
        assert_eq!(body["overflow"], false);
        let ids: Vec<&str> = body["candidates"]
            .as_array()
            .unwrap()
            .iter()
            .map(|c| c["id"].as_str().unwrap())
            .collect();
        assert_eq!(ids, ["id-a", "id-b", "id-c"]);
        assert_eq!(body["candidates"][0]["source"]["row_group"], 12);
        assert_eq!(body["data_version"], "2026-07-13.0");
        assert_eq!(body["normalization_version"], NORMALIZATION_VERSION);
    }

    #[test]
    fn signals_bounded_overflow_instead_of_truncating() {
        let candidates: Vec<AddressPageRecord> = (0..=CANDIDATE_CAP)
            .map(|i| record(&format!("id-{i}"), "10"))
            .collect();
        assert_eq!(candidates.len(), CANDIDATE_CAP + 1);
        let (status, body) = resolved_body("2026-07-13.0", &candidates, None);
        assert_eq!(status, 413);
        assert_eq!(body["error"], "address_candidate_overflow");
        assert_eq!(body["observed_candidates"], CANDIDATE_CAP + 1);
        assert_eq!(body["candidate_cap"], CANDIDATE_CAP);
        assert!(body.get("candidates").is_none());
    }

    #[test]
    fn debug_metrics_are_opt_in() {
        let candidates = vec![record("id-a", "10")];
        let (_, plain) = resolved_body("2026-07-13.0", &candidates, None);
        assert!(plain.get("debug").is_none());
        let (_, debug) = resolved_body(
            "2026-07-13.0",
            &candidates,
            Some(&RangeReadMetrics::default()),
        );
        assert!(debug["debug"]["read_metrics"].is_object());
    }

    // --- Route-level outcome mapping ---

    #[test]
    fn family_unavailable_outcome_is_a_stable_404_without_version() {
        let (status, version, body) = shape_outcome(AddressOutcome::FamilyUnavailable, false);
        assert_eq!(status, 404);
        assert_eq!(version, None);
        assert_eq!(body, json!({ "error": "address_family_unavailable" }));
    }

    #[test]
    fn out_of_coverage_outcome_is_200_with_version_and_coverage() {
        let (status, version, body) = shape_outcome(
            AddressOutcome::OutOfCoverage {
                data_version: "2026-07-13.0".to_string(),
            },
            false,
        );
        assert_eq!(status, 200);
        assert_eq!(version.as_deref(), Some("2026-07-13.0"));
        assert_eq!(body["coverage"], "out_of_coverage");
    }

    #[test]
    fn resolved_outcome_echoes_version_and_candidates() {
        let (status, version, body) = shape_outcome(
            AddressOutcome::Resolved {
                data_version: "2026-07-13.0".to_string(),
                candidates: vec![record("id-a", "10")],
                read_metrics: RangeReadMetrics::default(),
            },
            false,
        );
        assert_eq!(status, 200);
        assert_eq!(version.as_deref(), Some("2026-07-13.0"));
        assert_eq!(body["candidate_count"], 1);
        assert_eq!(body["coverage"], "in_coverage");
    }

    /// Reader-level tie-in: decode the committed cross-language page fixture and
    /// shape those real records through the response builder.
    #[test]
    fn shapes_candidates_decoded_from_the_committed_page_fixture() {
        let plain = include_bytes!("../../../tests/fixtures/pages/plain_page.bin");
        let records = crate::address_pages::decode_useful_page(plain).unwrap();
        assert!(!records.is_empty());
        let (status, body) = resolved_body("2026-07-13.0", &records, None);
        assert_eq!(status, 200);
        assert_eq!(body["candidate_count"], records.len());
        assert_eq!(body["candidates"][0]["street"], records[0].street.as_str());
        assert_eq!(body["candidates"][0]["id"], records[0].id.as_str());
    }
}
