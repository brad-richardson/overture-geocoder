//! Unified v2 geocoding API and atomic release discovery.

use std::cell::RefCell;
use std::collections::{HashMap, HashSet};
use std::rc::Rc;

use geocoder_core::query::NormalizedQuery;
use geocoder_core::{GeocoderQuery, IdLookupResult, LocationBias};
use serde::de::DeserializeOwned;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use worker::*;

use crate::address::MAX_ADDRESS_COLLECTION_BYTES;
use crate::address::{build_lookup_key, AddressOutcome};
use crate::address_construction_v1::{
    AddressRouting, ADDRESS_CONSTRUCTION_FORMAT, ADDRESS_CONSTRUCTION_NORMALIZATION_VERSION,
    MAX_ADDRESS_ROUTING_BYTES,
};
use crate::places_construction_v1::{
    compose_entity_phrase_candidates, construction_cell, entity_phrase_key,
    entity_phrase_token_groups, head_shard_id, head_shard_lookup, merge_head_candidates,
    merge_routed_candidates, neighbor_construction_cells, prefix_head_fallback_split,
    record_projection, retain_records_proving_dropped_tokens, retain_records_proving_query_run,
    routed_fetch_plan, supports_places_construction_format, validate_entity_phrase_records,
    HeadRoutingManifest, PlacesRouting, HEAD_QUERY_TOKEN_CAP, MAX_HEAD_SHARD_BYTES,
    MAX_PLACES_HEAD_ROUTING_BYTES, MAX_PLACES_ROUTING_BYTES, ROUTED_QUERY_TOKEN_CAP,
};
use crate::places_pages::{
    cjk_query_expansion, fold_for_scoring, query_terms, PlaceProjection, PlacesClause,
    MAX_CATALOG_OBJECT_BYTES, TOKENIZER_VERSION,
};
use crate::reverse_construction_v1::{ReverseFamily, ReverseHit};
use crate::stac::cache::{CATALOG_CACHE_TTL, IMMUTABLE_CACHE_TTL, TEXT_MEMO_TTL_MS};
use crate::stac::{ShardLoader, UserLocation, NOT_FOUND_SENTINEL};

const CATALOG_SCHEMA: &str = "overture-geocoder-v2-catalog-v1";
const UNAVAILABLE_CATALOG_SCHEMA: &str = "overture-geocoder-v2-unavailable-v1";
const UNAVAILABLE_REASON: &str = "operator-recovery";
const RELEASE_SCHEMA: &str = "overture-geocoder-v2-release-v1";
const PLACES_FORMAT_VERSION: &str = "PCSH0001";
const ADDRESS_FORMAT_VERSION: &str = "address-reduce-2";
const ADDRESS_NORMALIZATION_VERSION: &str = "nfc-uniws-collapse-ascii-lower-1";
const MAX_CATALOG_RELEASES: usize = 64;
const MAX_V2_CATALOG_BYTES: usize = 1024 * 1024;
const MAX_V2_RELEASE_BYTES: usize = 1024 * 1024;
const MAX_READINESS_MANIFEST_BYTES: u64 = 32 * 1024 * 1024;
const MAX_PLACES_FAMILY_MANIFEST_BYTES: usize = 8 * 1024 * 1024;
const MAX_REVERSE_CATALOG_OBJECT_BYTES: usize = 688;
const MAX_FAMILY_MANIFEST_ARTIFACTS: usize = 65_536;
const FAMILY_MANIFEST_SCHEMA: &str = "overture-global-family-manifest-v1";
const PLACES_HEAD_ARTIFACT_KEY: &str = "families/places/head.phrp";
const CAPABILITY_INVALID_SENTINEL: &str = "[v2-capability-invalid]";
const V2_RELEASE_CACHE_MAX_ENTRIES: usize = 1;
const MAX_QUERY_BYTES: usize = 200;
const MAX_RESULTS: usize = 10;
const ADDRESS_CANDIDATE_CAP: usize = 512;
const DIVISION_TYPES: &[&str] = &[
    "country",
    "region",
    "county",
    "localadmin",
    "locality",
    "neighborhood",
    "macrohood",
];
const LOCALITY_SUFFIX_TYPES: &[&str] = &["localadmin", "locality", "neighborhood", "macrohood"];

thread_local! {
    /// Short-lived mutable discovery pointer, aligned with the text memo TTL.
    /// Warm requests therefore skip both catalog/release parsing and R2 HEADs.
    static V2_LIVE_RELEASE: RefCell<Option<(String, Rc<V2Release>, u64)>> =
        const { RefCell::new(None) };
    /// Fully validated and readiness-probed immutable v2 release.
    /// Caching here removes repeat JSON parsing and, importantly, keeps the
    /// completion-marker HEAD probes off the per-request warm path.
    static V2_RELEASE_CACHE: RefCell<Vec<(String, Rc<V2Release>)>> =
        const { RefCell::new(Vec::new()) };
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
pub(crate) struct DataVersion {
    pub overture_release: String,
    pub geocoder_build: String,
}

#[derive(Debug, Deserialize)]
struct CatalogEntry {
    geocoder_build: String,
    overture_release: String,
    manifest_key: String,
    manifest_sha256: String,
    release_digest: String,
}

#[derive(Debug, Deserialize)]
struct V2Catalog {
    schema: String,
    latest: String,
    releases: Vec<CatalogEntry>,
    catalog_digest: String,
}

#[derive(Debug, Deserialize)]
struct LegacyCore {
    version: String,
    manifest_key: String,
    manifest_sha256: String,
    entrypoints: HashMap<String, String>,
}

#[derive(Debug, Clone, Deserialize)]
pub(crate) struct ArtifactIdentity {
    pub object_key: String,
    pub(crate) bytes: usize,
    pub(crate) sha256: String,
}

#[derive(Debug, Deserialize)]
struct FamilySource {
    kind: String,
    version: String,
    manifest_key: String,
    manifest_sha256: String,
}

#[derive(Debug, Deserialize, PartialEq, Eq)]
struct FamilyVersions {
    format: String,
    tokenizer: Option<String>,
    normalization: Option<String>,
}

#[derive(Debug, Deserialize)]
struct OperationSource {
    kind: String,
    version: String,
    request_sha256: String,
    slice_claim: ArtifactIdentity,
}

#[derive(Debug, Deserialize)]
struct FamilyReference {
    source: FamilySource,
    manifest_key: String,
    manifest_digest: String,
    manifest_sha256: String,
    versions: FamilyVersions,
    coverage: Value,
    operations: Vec<String>,
    entrypoints: HashMap<String, ArtifactIdentity>,
    #[serde(default)]
    operation_sources: HashMap<String, OperationSource>,
}

impl FamilyReference {
    fn operation_version(&self, operation: &str) -> &str {
        self.operation_sources
            .get(operation)
            .map_or(self.source.version.as_str(), |source| {
                source.version.as_str()
            })
    }
}

#[derive(Debug, Deserialize)]
pub(crate) struct V2Release {
    schema: String,
    geocoder_build: String,
    overture_release: String,
    data_version: DataVersion,
    legacy_core: LegacyCore,
    families: HashMap<String, FamilyReference>,
    operations: HashMap<String, Vec<String>>,
    release_digest: String,
}

impl V2Release {
    fn core_version(&self) -> &str {
        &self.legacy_core.version
    }

    fn supports(&self, operation: &str, family: &str) -> bool {
        self.operations
            .get(operation)
            .is_some_and(|families| families.iter().any(|value| value == family))
    }
}

fn valid_build(value: &str) -> bool {
    let Some((date, sequence)) = value.split_once('.') else {
        return false;
    };
    date.len() == 10
        && date.bytes().enumerate().all(|(index, byte)| match index {
            4 | 7 => byte == b'-',
            _ => byte.is_ascii_digit(),
        })
        && !sequence.is_empty()
        && sequence.bytes().all(|byte| byte.is_ascii_digit())
}

fn build_sort_key(value: &str) -> Option<(&str, u64)> {
    let (date, sequence) = value.split_once('.')?;
    if !valid_build(value) {
        return None;
    }
    Some((date, sequence.parse().ok()?))
}

fn valid_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

/// Reject strings whose Python and serde_json encodings can differ before
/// reproducing the producer's canonical digest. Current control documents use
/// printable ASCII identifiers and timestamps; failing closed here keeps the
/// cross-language hash contract exact instead of silently accepting a second
/// canonicalization for Unicode or control characters.
fn printable_ascii_document(value: &Value) -> bool {
    match value {
        Value::String(value) => value
            .chars()
            .all(|character| character.is_ascii() && !character.is_ascii_control()),
        Value::Array(values) => values.iter().all(printable_ascii_document),
        Value::Object(values) => values.iter().all(|(key, value)| {
            key.chars()
                .all(|character| character.is_ascii() && !character.is_ascii_control())
                && printable_ascii_document(value)
        }),
        Value::Null | Value::Bool(_) | Value::Number(_) => true,
    }
}

/// Reproduce `global_build_manifest.digest`: sorted, compact JSON plus one
/// trailing newline. serde_json's default Map is key-sorted in this workspace
/// (`preserve_order` is not enabled), matching Python's `sort_keys=True`.
fn canonical_control_digest(value: &Value) -> std::result::Result<String, String> {
    if !printable_ascii_document(value) {
        return Err("v2 control documents must contain only printable ASCII strings".into());
    }
    let mut canonical = serde_json::to_vec(value)
        .map_err(|error| format!("Failed to canonicalize v2 control document: {error}"))?;
    canonical.push(b'\n');
    Ok(format!("{:x}", Sha256::digest(&canonical)))
}

fn parse_verified_control_document<T: DeserializeOwned>(
    text: &str,
    digest_field: &str,
    label: &str,
) -> std::result::Result<T, String> {
    let mut value: Value =
        serde_json::from_str(text).map_err(|error| format!("Invalid {label}: {error}"))?;
    let expected = value
        .as_object_mut()
        .ok_or_else(|| format!("Invalid {label}: expected a JSON object"))?
        .remove(digest_field)
        .and_then(|value| value.as_str().map(str::to_owned))
        .filter(|value| valid_sha256(value))
        .ok_or_else(|| format!("Invalid {label}: {digest_field} is not a SHA-256 digest"))?;
    let actual = canonical_control_digest(&value)?;
    if actual != expected {
        return Err(format!(
            "Invalid {label}: {digest_field} differs from its contents"
        ));
    }
    value
        .as_object_mut()
        .expect("control document object was checked above")
        .insert(digest_field.to_string(), Value::String(expected));
    serde_json::from_value(value).map_err(|error| format!("Invalid {label}: {error}"))
}

fn safe_key(value: &str) -> bool {
    !value.is_empty()
        && !value.starts_with('/')
        && value.len() <= 512
        && value.split('/').all(|component| {
            !component.is_empty()
                && component != "."
                && component != ".."
                && component
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.'))
        })
}

fn valid_coverage(value: &Value) -> bool {
    let Some(object) = value.as_object() else {
        return false;
    };
    if object.len() != 3
        || object
            .get("name")
            .and_then(Value::as_str)
            .is_none_or(str::is_empty)
        || !object
            .get("bbox_scope")
            .and_then(Value::as_str)
            .is_some_and(|scope| matches!(scope, "exact" | "row_group_approximate"))
    {
        return false;
    }
    let Some(bbox) = object.get("bbox").and_then(Value::as_array) else {
        return false;
    };
    let coordinates = bbox.iter().map(Value::as_f64).collect::<Option<Vec<_>>>();
    coordinates.is_some_and(|bbox| {
        bbox.len() == 4
            && bbox.iter().all(|value| value.is_finite())
            && -180.0 <= bbox[0]
            && bbox[0] < bbox[2]
            && bbox[2] <= 180.0
            && -90.0 <= bbox[1]
            && bbox[1] < bbox[3]
            && bbox[3] <= 90.0
    })
}

fn release_manifest_key_for_catalog(catalog_key: &str, build: &str) -> Option<String> {
    if catalog_key == "v2/catalog.json" {
        return Some(format!("v2/releases/{build}/release.json"));
    }
    let parts = catalog_key.split('/').collect::<Vec<_>>();
    let valid_run = parts.get(1).is_some_and(|run| {
        !run.is_empty()
            && run.len() <= 128
            && run
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
    });
    (parts.len() == 3 && parts[0] == "smoketest-v2" && valid_run && parts[2] == "catalog.json")
        .then(|| format!("smoketest-v2/{}/release.json", parts[1]))
}

fn validate_catalog<'a>(
    catalog: &'a V2Catalog,
    catalog_key: &str,
) -> std::result::Result<&'a CatalogEntry, String> {
    let preview = catalog_key != "v2/catalog.json";
    if catalog.schema != CATALOG_SCHEMA
        || !valid_build(&catalog.latest)
        || !valid_sha256(&catalog.catalog_digest)
        || catalog.releases.is_empty()
        || catalog.releases.len() > MAX_CATALOG_RELEASES
        || (preview && catalog.releases.len() != 1)
        || catalog.releases[0].geocoder_build != catalog.latest
    {
        return Err("unsupported v2 catalog contract".into());
    }
    let mut builds = HashSet::new();
    let mut previous: Option<(&str, u64)> = None;
    for entry in &catalog.releases {
        let key = build_sort_key(&entry.geocoder_build)
            .ok_or_else(|| "invalid v2 catalog build".to_string())?;
        if previous.is_some_and(|old| old <= key)
            || !builds.insert(entry.geocoder_build.as_str())
            || entry.overture_release.is_empty()
            || release_manifest_key_for_catalog(catalog_key, &entry.geocoder_build)
                .is_none_or(|expected| entry.manifest_key != expected)
            || !safe_key(&entry.manifest_key)
            || !valid_sha256(&entry.manifest_sha256)
            || !valid_sha256(&entry.release_digest)
        {
            return Err("invalid v2 catalog release entry".into());
        }
        previous = Some(key);
    }
    Ok(&catalog.releases[0])
}

fn parse_catalog_control(
    text: &str,
    catalog_key: &str,
) -> std::result::Result<Option<V2Catalog>, String> {
    let value: Value = parse_verified_control_document(text, "catalog_digest", catalog_key)?;
    if value.get("schema").and_then(Value::as_str) != Some(UNAVAILABLE_CATALOG_SCHEMA) {
        let catalog = serde_json::from_value(value)
            .map_err(|error| format!("Invalid {catalog_key}: {error}"))?;
        return Ok(Some(catalog));
    }
    if catalog_key != "v2/catalog.json" {
        return Err("an unavailable v2 catalog is allowed only in production".into());
    }
    let object = value
        .as_object()
        .ok_or_else(|| format!("Invalid {catalog_key}: expected a JSON object"))?;
    let exact_fields = [
        "schema",
        "generated_at",
        "previous_catalog_sha256",
        "reason",
        "catalog_digest",
    ];
    if object.len() != exact_fields.len()
        || exact_fields
            .iter()
            .any(|field| !object.contains_key(*field))
        || object
            .get("generated_at")
            .and_then(Value::as_str)
            .is_none_or(str::is_empty)
        || object
            .get("previous_catalog_sha256")
            .and_then(Value::as_str)
            .is_none_or(|value| !valid_sha256(value))
        || object.get("reason").and_then(Value::as_str) != Some(UNAVAILABLE_REASON)
    {
        return Err("unsupported unavailable v2 catalog contract".into());
    }
    Ok(None)
}

fn validate_family(
    name: &str,
    family: &FamilyReference,
    overture_release: &str,
) -> std::result::Result<(), String> {
    let valid_source = match family.source.kind.as_str() {
        "core_release" => {
            valid_build(&family.source.version)
                && family.source.manifest_key
                    == format!("{}/release-manifest.json", family.source.version)
        }
        "family_slice" => {
            family
                .source
                .version
                .strip_prefix("slice-")
                .is_some_and(valid_build)
                && family.source.manifest_key
                    == format!("{}/slice-manifest.json", family.source.version)
        }
        _ => false,
    };
    if !matches!(name, "places" | "addresses")
        || !valid_source
        || !safe_key(&family.source.manifest_key)
        || !valid_sha256(&family.source.manifest_sha256)
        || family.manifest_key
            != format!(
                "{}/families/{name}/family-manifest.json",
                family.source.version
            )
        || !valid_sha256(&family.manifest_digest)
        || !valid_sha256(&family.manifest_sha256)
        || !valid_coverage(&family.coverage)
        || family.operations.is_empty()
    {
        return Err(format!("invalid v2 {name} family reference"));
    }
    let operation_set: HashSet<_> = family.operations.iter().map(String::as_str).collect();
    if operation_set.len() != family.operations.len()
        || operation_set
            != family
                .entrypoints
                .keys()
                .map(String::as_str)
                .collect::<HashSet<_>>()
    {
        return Err(format!("v2 {name} operations differ from entrypoints"));
    }
    if family
        .operation_sources
        .keys()
        .any(|operation| operation != "reverse" || !operation_set.contains(operation.as_str()))
    {
        return Err(format!("invalid v2 {name} external operation source"));
    }
    for (operation, source) in &family.operation_sources {
        let claim_payload = format!(
            concat!(
                "{{\"family\":\"{}\",\"overture_release\":\"{}\",",
                "\"request_sha256\":\"{}\",",
                "\"schema\":\"overture-construction-slice-claim-v1\",",
                "\"version\":\"{}\"}}\n"
            ),
            name, overture_release, source.request_sha256, source.version
        );
        let claim_sha256 = format!("{:x}", Sha256::digest(claim_payload.as_bytes()));
        if operation != "reverse"
            || source.kind != "reverse_slice"
            || source.version == family.source.version
            || !source
                .version
                .strip_prefix("slice-")
                .is_some_and(valid_build)
            || !valid_sha256(&source.request_sha256)
            || source.slice_claim.object_key != format!("{}/claims/{name}.json", source.version)
            || source.slice_claim.bytes != claim_payload.len()
            || source.slice_claim.sha256 != claim_sha256
        {
            return Err(format!("invalid v2 {name} {operation} operation source"));
        }
    }
    // The promoted construction formats (`PLRV0002+PLHD0002` and
    // `PLRV0003+PLHD0003` for Places -- 0003 adds the prominence byte, and both
    // are served so a deploy never outruns a rebuild -- and
    // `OAV1ART` for addresses) are accepted only for `family_slice` sources;
    // every other source keeps the exact PCSH0001 / address-reduce-2
    // contract. A construction format on a core_release source, a legacy
    // entrypoint under a construction format (or the reverse), or any unknown
    // format string all fail closed below.
    let places_construction = name == "places"
        && family.source.kind == "family_slice"
        && supports_places_construction_format(&family.versions.format);
    let address_construction = name == "addresses"
        && family.source.kind == "family_slice"
        && family.versions.format == ADDRESS_CONSTRUCTION_FORMAT;
    let expected_address_normalization = if address_construction {
        ADDRESS_CONSTRUCTION_NORMALIZATION_VERSION
    } else {
        ADDRESS_NORMALIZATION_VERSION
    };
    let valid_places_operations = name != "places"
        || (operation_set.contains("forward")
            && operation_set
                .iter()
                .all(|operation| matches!(*operation, "forward" | "reverse")));
    let valid_address_operations = name != "addresses"
        || (operation_set.contains("structured_forward")
            && operation_set
                .iter()
                .all(|operation| matches!(*operation, "structured_forward" | "reverse")));
    if (name == "places"
        && ((!places_construction && family.versions.format != PLACES_FORMAT_VERSION)
            || !valid_places_operations
            || family.versions.tokenizer.as_deref() != Some(TOKENIZER_VERSION)
            || family.versions.normalization.is_some()))
        || (name == "addresses"
            && ((!address_construction && family.versions.format != ADDRESS_FORMAT_VERSION)
                || !valid_address_operations
                || family.versions.normalization.as_deref()
                    != Some(expected_address_normalization)
                || family.versions.tokenizer.is_some()))
    {
        return Err(format!("v2 {name} family versions are unsupported"));
    }
    let places_entrypoint_key = if places_construction {
        format!("{}/families/places/routing.json", family.source.version)
    } else {
        format!("{}/families/places/catalog.pcat", family.source.version)
    };
    let places_entrypoint_cap = if places_construction {
        MAX_PLACES_ROUTING_BYTES
    } else {
        MAX_CATALOG_OBJECT_BYTES
    };
    let address_entrypoint_key = if address_construction {
        format!("{}/families/addresses/routing.json", family.source.version)
    } else {
        format!(
            "{}/families/addresses/address-collection.json",
            family.source.version
        )
    };
    let address_entrypoint_cap = if address_construction {
        MAX_ADDRESS_ROUTING_BYTES
    } else {
        MAX_ADDRESS_COLLECTION_BYTES
    };
    for (operation, identity) in &family.entrypoints {
        let entrypoint_version = family.operation_version(operation);
        let prefix = format!("{entrypoint_version}/families/{name}/");
        let (expected_key, maximum_bytes) = match (name, operation.as_str()) {
            ("places", "forward") => (places_entrypoint_key.clone(), places_entrypoint_cap),
            ("addresses", "structured_forward") => {
                (address_entrypoint_key.clone(), address_entrypoint_cap)
            }
            ("places" | "addresses", "reverse") => (
                format!(
                    "{}/families/{name}/reverse-catalog.rcat",
                    entrypoint_version
                ),
                MAX_REVERSE_CATALOG_OBJECT_BYTES,
            ),
            _ => return Err(format!("invalid v2 {name} operation")),
        };
        if !identity.object_key.starts_with(&prefix)
            || !safe_key(&identity.object_key)
            || identity.bytes == 0
            || !valid_sha256(&identity.sha256)
            || identity.bytes > maximum_bytes
            || (operation == "reverse" && identity.bytes != MAX_REVERSE_CATALOG_OBJECT_BYTES)
            || identity.object_key != expected_key
        {
            return Err(format!("invalid v2 {name} {operation} entrypoint"));
        }
    }
    Ok(())
}

fn validate_release(
    release: &V2Release,
    catalog: &CatalogEntry,
) -> std::result::Result<(), String> {
    if release.schema != RELEASE_SCHEMA
        || release.geocoder_build != catalog.geocoder_build
        || release.overture_release != catalog.overture_release
        || release.release_digest != catalog.release_digest
        || release.data_version
            != (DataVersion {
                overture_release: release.overture_release.clone(),
                geocoder_build: release.geocoder_build.clone(),
            })
        || !valid_build(&release.legacy_core.version)
        || release.legacy_core.manifest_key
            != format!("{}/release-manifest.json", release.legacy_core.version)
        || !valid_sha256(&release.legacy_core.manifest_sha256)
    {
        return Err("unsupported v2 release contract".into());
    }
    let expected_core = HashMap::from([
        (
            "feature_lookup".to_string(),
            format!("{}/id-collection.json", release.legacy_core.version),
        ),
        (
            "forward".to_string(),
            format!("{}/collection.json", release.legacy_core.version),
        ),
        (
            "reverse".to_string(),
            format!("{}/reverse-collection.json", release.legacy_core.version),
        ),
    ]);
    if release.legacy_core.entrypoints != expected_core
        || !release.supports("feature_lookup", "id")
        || !release.supports("forward", "divisions")
        || !release.supports("reverse", "divisions")
    {
        return Err("v2 release core capabilities are inconsistent".into());
    }
    for (name, family) in &release.families {
        validate_family(name, family, &release.overture_release)
            .map_err(|error| format!("{CAPABILITY_INVALID_SENTINEL} {error}"))?;
        for operation in &family.operations {
            if !release.supports(operation, name) {
                return Err(format!(
                    "{CAPABILITY_INVALID_SENTINEL} v2 {name} capability is absent at top level"
                ));
            }
        }
    }
    let mut expected_operations = HashMap::from([
        ("feature_lookup".to_string(), vec!["id".to_string()]),
        ("forward".to_string(), vec!["divisions".to_string()]),
        ("reverse".to_string(), vec!["divisions".to_string()]),
    ]);
    for (family_name, family) in &release.families {
        for operation in &family.operations {
            expected_operations
                .entry(operation.clone())
                .or_default()
                .push(family_name.clone());
        }
    }
    for families in expected_operations.values_mut() {
        families.sort();
    }
    if release.operations != expected_operations {
        return Err("v2 top-level operations differ from supported capabilities".into());
    }
    let mut source_identities: HashMap<&str, (&str, &str)> = HashMap::new();
    for family in release.families.values() {
        let identity = (
            family.source.manifest_key.as_str(),
            family.source.manifest_sha256.as_str(),
        );
        if source_identities
            .insert(family.source.version.as_str(), identity)
            .is_some_and(|previous| previous != identity)
        {
            return Err("v2 families disagree about one source version".into());
        }
    }
    Ok(())
}

#[derive(Debug, PartialEq, Eq)]
struct ReadinessObject {
    key: String,
    expected_bytes: Option<u64>,
    expected_sha256: String,
}

#[derive(Deserialize)]
struct ServingFamilyManifest {
    schema: String,
    family: String,
    manifest_digest: String,
    artifacts: Vec<ServingFamilyArtifact>,
}

#[derive(Deserialize)]
struct ServingFamilyArtifact {
    object_key: String,
    bytes: u64,
    sha256: String,
}

fn release_readiness_objects(release: &V2Release) -> Vec<ReadinessObject> {
    let mut objects = vec![ReadinessObject {
        key: release.legacy_core.manifest_key.clone(),
        expected_bytes: None,
        expected_sha256: release.legacy_core.manifest_sha256.clone(),
    }];
    let mut source_keys = HashSet::new();
    for (family_name, family) in &release.families {
        if source_keys.insert(family.source.manifest_key.as_str()) {
            objects.push(ReadinessObject {
                key: family.source.manifest_key.clone(),
                expected_bytes: None,
                expected_sha256: family.source.manifest_sha256.clone(),
            });
        }
        // The Places manifest — and the addresses manifest on the promoted
        // construction format — is loaded under a bounded parse below so its
        // attested artifact identities can be extracted before admission.
        // Other family manifests only need their release-pinned streaming
        // identity.
        let bounded_parse = family_name == "places"
            || (family_name == "addresses"
                && family.versions.format == ADDRESS_CONSTRUCTION_FORMAT);
        if !bounded_parse {
            objects.push(ReadinessObject {
                key: family.manifest_key.clone(),
                expected_bytes: None,
                expected_sha256: family.manifest_sha256.clone(),
            });
        }
        objects.extend(family.entrypoints.values().map(|identity| ReadinessObject {
            key: identity.object_key.clone(),
            expected_bytes: Some(identity.bytes as u64),
            expected_sha256: identity.sha256.clone(),
        }));
        objects.extend(
            family
                .operation_sources
                .values()
                .map(|source| ReadinessObject {
                    key: source.slice_claim.object_key.clone(),
                    expected_bytes: Some(source.slice_claim.bytes as u64),
                    expected_sha256: source.slice_claim.sha256.clone(),
                }),
        );
    }
    objects
}

fn places_head_requirement(
    manifest_text: &str,
    family: &FamilyReference,
) -> std::result::Result<ReadinessObject, String> {
    let actual_manifest_sha = format!("{:x}", Sha256::digest(manifest_text.as_bytes()));
    if actual_manifest_sha != family.manifest_sha256 {
        return Err("v2 Places family manifest SHA-256 differs from release".into());
    }
    let manifest: ServingFamilyManifest = serde_json::from_str(manifest_text)
        .map_err(|error| format!("Invalid v2 Places family manifest: {error}"))?;
    if manifest.schema != FAMILY_MANIFEST_SCHEMA
        || manifest.family != "places"
        || manifest.manifest_digest != family.manifest_digest
        || manifest.artifacts.is_empty()
        || manifest.artifacts.len() > MAX_FAMILY_MANIFEST_ARTIFACTS
    {
        return Err("unsupported v2 Places family manifest contract".into());
    }
    let mut heads = manifest
        .artifacts
        .into_iter()
        .filter(|artifact| artifact.object_key == PLACES_HEAD_ARTIFACT_KEY);
    let head = heads
        .next()
        .ok_or_else(|| "v2 Places family manifest omits canonical head.phrp".to_string())?;
    if heads.next().is_some() || head.bytes == 0 || !valid_sha256(&head.sha256) {
        return Err("v2 Places family manifest has an invalid canonical head identity".into());
    }
    Ok(ReadinessObject {
        key: format!("{}/{}", family.source.version, PLACES_HEAD_ARTIFACT_KEY),
        expected_bytes: Some(head.bytes),
        expected_sha256: head.sha256,
    })
}

/// `object_key -> (bytes, sha256)` identities a family manifest attests.
type AttestedArtifacts = HashMap<String, (u64, String)>;

/// Validate a promoted construction family manifest against the release
/// reference and return its attested `object_key -> (bytes, sha256)` map.
fn family_manifest_artifacts(
    manifest_text: &str,
    family: &FamilyReference,
    name: &str,
) -> std::result::Result<AttestedArtifacts, String> {
    let actual_manifest_sha = format!("{:x}", Sha256::digest(manifest_text.as_bytes()));
    if actual_manifest_sha != family.manifest_sha256 {
        return Err(format!(
            "v2 {name} family manifest SHA-256 differs from release"
        ));
    }
    let manifest: ServingFamilyManifest = serde_json::from_str(manifest_text)
        .map_err(|error| format!("Invalid v2 {name} family manifest: {error}"))?;
    if manifest.schema != FAMILY_MANIFEST_SCHEMA
        || manifest.family != name
        || manifest.manifest_digest != family.manifest_digest
        || manifest.artifacts.is_empty()
        || manifest.artifacts.len() > MAX_FAMILY_MANIFEST_ARTIFACTS
    {
        return Err(format!("unsupported v2 {name} family manifest contract"));
    }
    let mut artifacts = HashMap::with_capacity(manifest.artifacts.len());
    for artifact in manifest.artifacts {
        if artifact.bytes == 0 || !valid_sha256(&artifact.sha256) || !safe_key(&artifact.object_key)
        {
            return Err(format!("invalid v2 {name} family manifest artifact"));
        }
        if artifacts
            .insert(artifact.object_key, (artifact.bytes, artifact.sha256))
            .is_some()
        {
            return Err(format!("v2 {name} family manifest repeats an artifact"));
        }
    }
    Ok(artifacts)
}

/// Require every release-advertised family entrypoint to be attested by the
/// family manifest under its source-relative key. The release readiness gate
/// still hashes each entrypoint from R2; this closes the other side of the
/// identity chain without duplicating the expensive data-object verification.
fn attest_family_entrypoints(
    artifacts: &AttestedArtifacts,
    family: &FamilyReference,
    name: &str,
) -> std::result::Result<(), String> {
    let source_prefix = format!("{}/", family.source.version);
    for (operation, entrypoint) in &family.entrypoints {
        if family.operation_sources.contains_key(operation) {
            continue;
        }
        let relative_key = entrypoint
            .object_key
            .strip_prefix(&source_prefix)
            .ok_or_else(|| format!("v2 {name} {operation} entrypoint is outside its source"))?;
        let attested = artifacts
            .get(relative_key)
            .ok_or_else(|| format!("v2 {name} family manifest omits its {operation} entrypoint"))?;
        if attested.0 != entrypoint.bytes as u64 || attested.1 != entrypoint.sha256 {
            return Err(format!(
                "v2 {name} {operation} identity differs between manifest and release"
            ));
        }
    }
    Ok(())
}

/// Admission for the promoted construction layout: authenticate the family
/// manifest, prove routing.json's bytes against the release-pinned entrypoint
/// identity, parse and validate the routing table, and require every object it
/// can route to (including the head routing manifest) to be attested.
fn places_construction_admission(
    manifest_text: &str,
    routing_text: &str,
    family: &FamilyReference,
) -> std::result::Result<(AttestedArtifacts, PlacesRouting), String> {
    let artifacts = family_manifest_artifacts(manifest_text, family, "places")?;
    attest_family_entrypoints(&artifacts, family, "places")?;
    let entrypoint = family
        .entrypoints
        .get("forward")
        .ok_or_else(|| "v2 Places construction family omits its forward entrypoint".to_string())?;
    let attested_routing = artifacts
        .get("families/places/routing.json")
        .ok_or_else(|| "v2 Places family manifest omits routing.json".to_string())?;
    if attested_routing.0 != entrypoint.bytes as u64 || attested_routing.1 != entrypoint.sha256 {
        return Err("v2 Places routing identity differs between manifest and release".into());
    }
    let actual_routing_sha = format!("{:x}", Sha256::digest(routing_text.as_bytes()));
    if routing_text.len() != entrypoint.bytes || actual_routing_sha != entrypoint.sha256 {
        return Err("v2 Places routing bytes differ from the release-pinned identity".into());
    }
    let routing = PlacesRouting::parse(routing_text)?;
    for name in routing.routed_object_names() {
        if !artifacts.contains_key(&format!("families/places/objects/{name}")) {
            return Err(format!(
                "v2 Places routing names an unattested object: {name}"
            ));
        }
    }
    if !artifacts.contains_key(&format!(
        "families/places/objects/{}",
        routing.head.manifest_object
    )) {
        return Err("v2 Places head routing manifest is not attested".into());
    }
    Ok((artifacts, routing))
}

/// Head admission for the promoted construction layout: authenticate the head
/// routing manifest bytes against the family-manifest attestation, check its
/// geometry against routing.json, require every populated shard's identity to
/// agree with the attestation, and emit the deterministic spot-check sample
/// (first, middle, and last populated shards in shard-id order) as readiness
/// objects that are then streamed-and-hashed from R2. Full verification of all
/// 4,096 shards (5.14 GB) is deliberately not done at admission; the per-shard
/// identities stay pinned by this manifest, whose own bytes are pinned by the
/// release-attested family manifest.
fn places_construction_head_requirements(
    head_manifest_text: &str,
    routing: &PlacesRouting,
    artifacts: &AttestedArtifacts,
    version: &str,
) -> std::result::Result<Vec<ReadinessObject>, String> {
    let attested = artifacts
        .get(&format!(
            "families/places/objects/{}",
            routing.head.manifest_object
        ))
        .ok_or_else(|| "v2 Places head routing manifest is not attested".to_string())?;
    let actual_sha = format!("{:x}", Sha256::digest(head_manifest_text.as_bytes()));
    if head_manifest_text.len() as u64 != attested.0 || actual_sha != attested.1 {
        return Err("v2 Places head routing manifest bytes differ from attestation".into());
    }
    let manifest = HeadRoutingManifest::parse(head_manifest_text)?;
    if !manifest.agrees_with(&routing.head) {
        return Err("v2 Places head routing manifest geometry differs from routing.json".into());
    }
    for shard in manifest.shards() {
        let attested = artifacts
            .get(&format!("families/places/objects/{}", shard.path))
            .ok_or_else(|| format!("v2 Places head shard is not attested: {}", shard.path))?;
        if attested.0 != shard.bytes || attested.1 != shard.sha256 {
            return Err(format!(
                "v2 Places head shard identity disagrees between manifests: {}",
                shard.path
            ));
        }
    }
    Ok(manifest
        .admission_sample()
        .into_iter()
        .map(|shard| ReadinessObject {
            key: format!("{version}/families/places/objects/{}", shard.path),
            expected_bytes: Some(shard.bytes),
            expected_sha256: shard.sha256.clone(),
        })
        .collect())
}

/// Individual admission sample cap for the addresses construction lane. Planet
/// `OAV1ART` artifacts approach the 2 GiB publication cap, so streaming-hash
/// verification is proportionate only for small objects; larger ones stay
/// pinned by the release-attested family manifest and are structurally
/// self-checked at serving time.
const MAX_ADDRESS_ADMISSION_SAMPLE_BYTES: u64 = 32 * 1024 * 1024;
const ADDRESS_ADMISSION_SAMPLE_OBJECTS: usize = 3;

/// Admission for the promoted construction addresses layout: authenticate the
/// family manifest, prove routing.json's bytes against the release-pinned
/// entrypoint identity, parse and validate the envelope table, and require
/// every object it can route to to be attested.
fn address_construction_admission(
    manifest_text: &str,
    routing_text: &str,
    family: &FamilyReference,
) -> std::result::Result<(AttestedArtifacts, AddressRouting), String> {
    let artifacts = family_manifest_artifacts(manifest_text, family, "addresses")?;
    attest_family_entrypoints(&artifacts, family, "addresses")?;
    let entrypoint = family
        .entrypoints
        .get("structured_forward")
        .ok_or_else(|| {
            "v2 addresses construction family omits its structured_forward entrypoint".to_string()
        })?;
    let attested_routing = artifacts
        .get("families/addresses/routing.json")
        .ok_or_else(|| "v2 addresses family manifest omits routing.json".to_string())?;
    if attested_routing.0 != entrypoint.bytes as u64 || attested_routing.1 != entrypoint.sha256 {
        return Err("v2 addresses routing identity differs between manifest and release".into());
    }
    let actual_routing_sha = format!("{:x}", Sha256::digest(routing_text.as_bytes()));
    if routing_text.len() != entrypoint.bytes || actual_routing_sha != entrypoint.sha256 {
        return Err("v2 addresses routing bytes differ from the release-pinned identity".into());
    }
    let routing = AddressRouting::parse(routing_text)?;
    for name in routing.routed_object_names() {
        if !artifacts.contains_key(&format!("families/addresses/objects/{name}")) {
            return Err(format!(
                "v2 addresses routing names an unattested object: {name}"
            ));
        }
    }
    Ok((artifacts, routing))
}

/// Deterministic admission spot-check sample for the addresses construction
/// layout: the up-to-three smallest routed objects (ordered by attested
/// `(bytes, name)`) that fit the per-object sample cap, streamed and hashed
/// from R2 against their attested identities. Full verification of all routed
/// objects (581 at planet scale, individually up to 2 GiB) is deliberately
/// NOT done at admission; every identity remains pinned by the family
/// manifest, whose own bytes are pinned by the release.
fn address_construction_sample(
    routing: &AddressRouting,
    artifacts: &AttestedArtifacts,
    version: &str,
) -> std::result::Result<Vec<ReadinessObject>, String> {
    let mut candidates = Vec::new();
    let names: HashSet<&str> = routing.routed_object_names().collect();
    for name in names {
        let (bytes, sha256) = artifacts
            .get(&format!("families/addresses/objects/{name}"))
            .ok_or_else(|| format!("v2 addresses routing names an unattested object: {name}"))?;
        if *bytes <= MAX_ADDRESS_ADMISSION_SAMPLE_BYTES {
            candidates.push((*bytes, name, sha256));
        }
    }
    candidates.sort();
    candidates.truncate(ADDRESS_ADMISSION_SAMPLE_OBJECTS);
    Ok(candidates
        .into_iter()
        .map(|(bytes, name, sha256)| ReadinessObject {
            key: format!("{version}/families/addresses/objects/{name}"),
            expected_bytes: Some(bytes),
            expected_sha256: sha256.clone(),
        })
        .collect())
}

fn readiness_identity_matches(
    requirement: &ReadinessObject,
    actual_bytes: u64,
    actual_sha256: &str,
) -> bool {
    requirement
        .expected_bytes
        .map_or(actual_bytes > 0, |expected| actual_bytes == expected)
        && actual_sha256 == requirement.expected_sha256
}

impl ShardLoader {
    async fn verified_places_head_requirement(
        &self,
        family: &FamilyReference,
    ) -> Result<ReadinessObject> {
        let manifest_text = self
            .memoized_get_bounded_text(
                &family.manifest_key,
                MAX_PLACES_FAMILY_MANIFEST_BYTES,
                IMMUTABLE_CACHE_TTL,
            )
            .await?
            .ok_or_else(|| crate::stac::not_found(&family.manifest_key))?;
        // Do not retain the raw proof document beside the parsed Places and
        // address routing structures. The edge cache still serves later cold
        // isolates; this isolate only retains the admitted release.
        self.forget_memoized_text(&family.manifest_key);
        places_head_requirement(&manifest_text, family).map_err(Error::RustError)
    }

    /// Admission for the promoted construction Places layout: authenticate the
    /// family manifest, routing.json, and head routing manifest identities,
    /// then stream-verify only the deterministic head-shard sample.
    async fn verify_places_construction_readiness(&self, family: &FamilyReference) -> Result<()> {
        let manifest_text = self
            .memoized_get_bounded_text(
                &family.manifest_key,
                MAX_PLACES_FAMILY_MANIFEST_BYTES,
                IMMUTABLE_CACHE_TTL,
            )
            .await?
            .ok_or_else(|| crate::stac::not_found(&family.manifest_key))?;
        self.forget_memoized_text(&family.manifest_key);
        let routing_key = format!("{}/families/places/routing.json", family.source.version);
        let routing_text = self
            .memoized_get_bounded_text(&routing_key, MAX_PLACES_ROUTING_BYTES, IMMUTABLE_CACHE_TTL)
            .await?
            .ok_or_else(|| crate::stac::not_found(&routing_key))?;
        self.forget_memoized_text(&routing_key);
        let (artifacts, routing) =
            places_construction_admission(&manifest_text, &routing_text, family)
                .map_err(Error::RustError)?;
        let head_key = format!(
            "{}/families/places/objects/{}",
            family.source.version, routing.head.manifest_object
        );
        let head_text = self
            .memoized_get_bounded_text(
                &head_key,
                MAX_PLACES_HEAD_ROUTING_BYTES,
                IMMUTABLE_CACHE_TTL,
            )
            .await?
            .ok_or_else(|| crate::stac::not_found(&head_key))?;
        self.forget_memoized_text(&head_key);
        let requirements = places_construction_head_requirements(
            &head_text,
            &routing,
            &artifacts,
            &family.source.version,
        )
        .map_err(Error::RustError)?;
        for requirement in requirements {
            self.verify_readiness_object(&requirement).await?;
        }
        Ok(())
    }

    /// Admission for the promoted construction addresses layout: authenticate
    /// the family manifest and routing.json identities, then stream-verify
    /// only the deterministic small-object sample.
    async fn verify_address_construction_readiness(&self, family: &FamilyReference) -> Result<()> {
        let manifest_text = self
            .memoized_get_bounded_text(
                &family.manifest_key,
                MAX_PLACES_FAMILY_MANIFEST_BYTES,
                IMMUTABLE_CACHE_TTL,
            )
            .await?
            .ok_or_else(|| crate::stac::not_found(&family.manifest_key))?;
        self.forget_memoized_text(&family.manifest_key);
        let routing_key = format!("{}/families/addresses/routing.json", family.source.version);
        let routing_text = self
            .memoized_get_bounded_text(&routing_key, MAX_ADDRESS_ROUTING_BYTES, IMMUTABLE_CACHE_TTL)
            .await?
            .ok_or_else(|| crate::stac::not_found(&routing_key))?;
        self.forget_memoized_text(&routing_key);
        let (artifacts, routing) =
            address_construction_admission(&manifest_text, &routing_text, family)
                .map_err(Error::RustError)?;
        let requirements =
            address_construction_sample(&routing, &artifacts, &family.source.version)
                .map_err(Error::RustError)?;
        for requirement in requirements {
            self.verify_readiness_object(&requirement).await?;
        }
        Ok(())
    }

    async fn verify_readiness_object(&self, requirement: &ReadinessObject) -> Result<()> {
        let max_bytes = requirement
            .expected_bytes
            .unwrap_or(MAX_READINESS_MANIFEST_BYTES);
        let (actual_bytes, actual_sha256) = self
            .immutable_object_identity(&requirement.key, max_bytes)
            .await?;
        let ready = readiness_identity_matches(requirement, actual_bytes, &actual_sha256);
        if !ready {
            return Err(Error::RustError(format!(
                "v2 readiness object identity is invalid: {}",
                requirement.key
            )));
        }
        Ok(())
    }

    async fn verify_v2_release_readiness(&self, release: &V2Release) -> Result<()> {
        for requirement in release_readiness_objects(release) {
            self.verify_readiness_object(&requirement).await?;
        }
        if let Some(places) = release.families.get("places") {
            if supports_places_construction_format(&places.versions.format) {
                self.verify_places_construction_readiness(places).await?;
            } else {
                let head = self.verified_places_head_requirement(places).await?;
                self.verify_readiness_object(&head).await?;
            }
        }
        if let Some(addresses) = release.families.get("addresses") {
            if addresses.versions.format == ADDRESS_CONSTRUCTION_FORMAT {
                self.verify_address_construction_readiness(addresses)
                    .await?;
            }
        }
        Ok(())
    }

    pub(crate) async fn load_v2_release(&self) -> Result<Rc<V2Release>> {
        let now = Date::now().as_millis();
        let catalog_key = self.v2_catalog_key().to_string();
        if let Some(release) = V2_LIVE_RELEASE.with(|cached| {
            cached
                .borrow()
                .as_ref()
                .filter(|(key, _, expires)| key == &catalog_key && *expires > now)
                .map(|(_, release, _)| Rc::clone(release))
        }) {
            return Ok(release);
        }

        let catalog_text = self
            .memoized_get_bounded_text(&catalog_key, MAX_V2_CATALOG_BYTES, CATALOG_CACHE_TTL)
            .await?
            .ok_or_else(|| crate::stac::not_found(&catalog_key))?;
        let Some(catalog) =
            parse_catalog_control(&catalog_text, &catalog_key).map_err(Error::RustError)?
        else {
            return Err(crate::stac::not_found(&catalog_key));
        };
        let entry = validate_catalog(&catalog, &catalog_key).map_err(Error::RustError)?;
        let cache_key = format!("{}#{}", entry.manifest_key, entry.manifest_sha256);
        let cached = V2_RELEASE_CACHE.with(|cache| {
            let mut cache = cache.borrow_mut();
            cache
                .iter()
                .position(|(candidate, _)| candidate == &cache_key)
                .map(|position| {
                    let entry = cache.remove(position);
                    let release = Rc::clone(&entry.1);
                    cache.push(entry);
                    release
                })
        });
        if let Some(release) = cached {
            V2_LIVE_RELEASE.with(|cached| {
                *cached.borrow_mut() = Some((
                    catalog_key.clone(),
                    Rc::clone(&release),
                    now.saturating_add(TEXT_MEMO_TTL_MS),
                ));
            });
            return Ok(release);
        }

        let manifest_text = self
            .memoized_get_bounded_text(
                &entry.manifest_key,
                MAX_V2_RELEASE_BYTES,
                IMMUTABLE_CACHE_TTL,
            )
            .await?
            .ok_or_else(|| crate::stac::not_found(&entry.manifest_key))?;
        let actual_sha = format!("{:x}", Sha256::digest(manifest_text.as_bytes()));
        if actual_sha != entry.manifest_sha256 {
            return Err(Error::RustError(
                "v2 release manifest SHA-256 differs from catalog".into(),
            ));
        }
        let release: V2Release =
            parse_verified_control_document(&manifest_text, "release_digest", &entry.manifest_key)
                .map_err(Error::RustError)?;
        validate_release(&release, entry).map_err(Error::RustError)?;
        self.verify_v2_release_readiness(&release).await?;
        let release = Rc::new(release);
        V2_RELEASE_CACHE.with(|cache| {
            let mut cache = cache.borrow_mut();
            if !cache.iter().any(|(candidate, _)| candidate == &cache_key) {
                cache.push((cache_key, Rc::clone(&release)));
                while cache.len() > V2_RELEASE_CACHE_MAX_ENTRIES {
                    cache.remove(0);
                }
            }
        });
        V2_LIVE_RELEASE.with(|cached| {
            *cached.borrow_mut() = Some((
                catalog_key,
                Rc::clone(&release),
                now.saturating_add(TEXT_MEMO_TTL_MS),
            ));
        });
        Ok(release)
    }
}

enum ReleaseAvailability {
    Ready(Rc<V2Release>),
    Unavailable(Response),
}

async fn load_available_release(loader: &ShardLoader) -> Result<ReleaseAvailability> {
    match loader.load_v2_release().await {
        Ok(release) => Ok(ReleaseAvailability::Ready(release)),
        Err(error) if format!("{error:?}").contains(CAPABILITY_INVALID_SENTINEL) => {
            Ok(ReleaseAvailability::Unavailable(json_error(
                "capability_unavailable",
                "the requested v2 capability is not currently available",
                503,
            )?))
        }
        Err(error) if format!("{error:?}").contains(NOT_FOUND_SENTINEL) => {
            Ok(ReleaseAvailability::Unavailable(json_error(
                "release_unavailable",
                "no v2 geocoder release is currently available",
                503,
            )?))
        }
        Err(error) => Err(error),
    }
}

pub async fn handle_preview_health(
    _req: Request,
    ctx: RouteContext<std::rc::Rc<Context>>,
) -> Result<Response> {
    let expected_build = ctx
        .env
        .var("EXPECTED_GEOCODER_BUILD")
        .map_err(|_| Error::RustError("preview health requires EXPECTED_GEOCODER_BUILD".into()))?
        .to_string();
    let expected_release = ctx
        .env
        .var("EXPECTED_OVERTURE_RELEASE")
        .map_err(|_| Error::RustError("preview health requires EXPECTED_OVERTURE_RELEASE".into()))?
        .to_string();
    let expected_catalog = ctx
        .env
        .var("EXPECTED_V2_CATALOG_KEY")
        .map_err(|_| Error::RustError("preview health requires EXPECTED_V2_CATALOG_KEY".into()))?
        .to_string();
    let loader = ShardLoader::with_context(&ctx.env, ctx.data.clone())?;
    let release = match load_available_release(&loader).await? {
        ReleaseAvailability::Ready(release) => release,
        ReleaseAvailability::Unavailable(response) => return Ok(response),
    };
    if release.geocoder_build != expected_build
        || release.overture_release != expected_release
        || loader.v2_catalog_key() != expected_catalog
    {
        return json_error(
            "candidate_identity_mismatch",
            "preview candidate identity differs from the expected build",
            503,
        );
    }
    let mut response = Response::from_json(&json!({
        "status": "ok",
        "geocoder_build": release.geocoder_build,
        "overture_release": release.overture_release,
        "catalog_key": loader.v2_catalog_key(),
        "candidate_isolated": true,
    }))?;
    response
        .headers_mut()
        .set("Content-Type", "application/json; charset=utf-8")?;
    Ok(response)
}

fn json_error(code: &str, message: &str, status: u16) -> Result<Response> {
    let mut response =
        Response::from_json(&json!({"error": code, "message": message}))?.with_status(status);
    response
        .headers_mut()
        .set("Content-Type", "application/json; charset=utf-8")?;
    Ok(response)
}

fn versioned_response(body: &Value, version: &DataVersion, status: u16) -> Result<Response> {
    let mut response = Response::from_json(body)?.with_status(status);
    response
        .headers_mut()
        .set("Content-Type", "application/geo+json; charset=utf-8")?;
    response
        .headers_mut()
        .set("X-Data-Version", &version.geocoder_build)?;
    response
        .headers_mut()
        .set("X-Geocoder-Build", &version.geocoder_build)?;
    response
        .headers_mut()
        .set("X-Overture-Release", &version.overture_release)?;
    Ok(response)
}

fn versioned_json_response(body: &Value, version: &DataVersion, status: u16) -> Result<Response> {
    let mut response = Response::from_json(body)?.with_status(status);
    response
        .headers_mut()
        .set("Content-Type", "application/json; charset=utf-8")?;
    response
        .headers_mut()
        .set("X-Data-Version", &version.geocoder_build)?;
    response
        .headers_mut()
        .set("X-Geocoder-Build", &version.geocoder_build)?;
    response
        .headers_mut()
        .set("X-Overture-Release", &version.overture_release)?;
    Ok(response)
}

fn parse_bool(value: Option<&String>, default: bool) -> std::result::Result<bool, String> {
    match value.map(String::as_str) {
        None => Ok(default),
        Some("1" | "true") => Ok(true),
        Some("0" | "false") => Ok(false),
        Some(_) => Err("boolean parameters accept true, false, 1, or 0".into()),
    }
}

fn parse_limit(value: Option<&String>) -> std::result::Result<usize, String> {
    let limit = value.map_or(Ok(MAX_RESULTS), |raw| {
        raw.parse::<usize>()
            .map_err(|_| "limit must be an integer".to_string())
    })?;
    if !(1..=MAX_RESULTS).contains(&limit) {
        return Err(format!("limit must be between 1 and {MAX_RESULTS}"));
    }
    Ok(limit)
}

fn parse_reverse_limit(value: Option<&String>) -> std::result::Result<usize, String> {
    let limit = value.map_or(Ok(1), |raw| {
        raw.parse::<usize>()
            .map_err(|_| "limit must be an integer".to_string())
    })?;
    if !(1..=MAX_RESULTS).contains(&limit) {
        return Err(format!("limit must be between 1 and {MAX_RESULTS}"));
    }
    Ok(limit)
}

fn parse_reverse_radius(
    value: Option<&String>,
    family: ReverseFamily,
) -> std::result::Result<u32, String> {
    let radius = value.map_or(Ok(family.default_radius_m()), |raw| {
        raw.parse::<u32>()
            .map_err(|_| "radius must be an integer number of metres".to_string())
    })?;
    if radius == 0 || radius > family.maximum_radius_m() {
        return Err(format!(
            "radius {radius} is outside the 1..={} metre range for {}",
            family.maximum_radius_m(),
            family.feature_type()
        ));
    }
    Ok(radius)
}

fn normalized_type(value: &str) -> String {
    match value.trim().to_lowercase().as_str() {
        "place" => "poi".into(),
        "neighbourhood" => "neighborhood".into(),
        other => other.into(),
    }
}

fn valid_gers_id(value: &str) -> bool {
    let bytes = value.as_bytes();
    let hyphenated = bytes.len() == 36;
    let shape_ok = bytes.len() == 32
        || (hyphenated && [8, 13, 18, 23].iter().all(|index| bytes[*index] == b'-'));
    shape_ok
        && bytes.iter().enumerate().all(|(index, byte)| {
            byte.is_ascii_hexdigit()
                || (hyphenated && matches!(index, 8 | 13 | 18 | 23) && *byte == b'-')
        })
}

fn parse_types(raw: Option<&String>) -> std::result::Result<HashSet<String>, String> {
    let defaults = DIVISION_TYPES
        .iter()
        .map(|value| (*value).to_string())
        .chain(["poi".to_string()])
        .collect();
    let Some(raw) = raw else {
        return Ok(defaults);
    };
    let types: HashSet<_> = raw
        .split(',')
        .map(normalized_type)
        .filter(|value| !value.is_empty())
        .collect();
    let supported: HashSet<_> = DIVISION_TYPES
        .iter()
        .copied()
        .chain(["poi", "address"])
        .collect();
    if types.is_empty()
        || types
            .iter()
            .any(|value| !supported.contains(value.as_str()))
    {
        return Err("types contains an unsupported feature type".into());
    }
    Ok(types)
}

fn parse_coordinate(
    raw: &str,
    name: &str,
    minimum: f64,
    maximum: f64,
) -> std::result::Result<f64, String> {
    let value = raw
        .parse::<f64>()
        .map_err(|_| format!("{name} must be a number"))?;
    if !value.is_finite() || !(minimum..=maximum).contains(&value) {
        return Err(format!("{name} is outside valid bounds"));
    }
    Ok(value)
}

fn parse_proximity(
    params: &HashMap<String, String>,
) -> std::result::Result<Option<(f64, f64)>, String> {
    let Some(raw) = params.get("proximity") else {
        return Ok(None);
    };
    let Some((longitude, latitude)) = raw.split_once(',') else {
        return Err("proximity must use longitude,latitude".into());
    };
    Ok(Some((
        parse_coordinate(longitude, "proximity longitude", -180.0, 180.0)?,
        parse_coordinate(latitude, "proximity latitude", -90.0, 90.0)?,
    )))
}

fn has_structured_address(params: &HashMap<String, String>) -> bool {
    ["street", "number", "address_number"]
        .iter()
        .any(|name| params.contains_key(*name))
}

fn address_params(params: &HashMap<String, String>) -> HashMap<String, String> {
    let aliases: [(&str, &[&str]); 8] = [
        ("country", &["country"]),
        (
            "admin_level_general",
            &["admin_level_general", "state", "region"],
        ),
        ("admin_level_specific", &["admin_level_specific", "county"]),
        ("postal_city", &["postal_city", "city"]),
        ("postcode", &["postcode", "postalcode"]),
        ("street", &["street"]),
        ("number", &["number", "address_number"]),
        ("unit", &["unit"]),
    ];
    aliases
        .into_iter()
        .map(|(target, sources)| {
            let value = sources
                .iter()
                .find_map(|source| params.get(*source))
                .cloned()
                .unwrap_or_default();
            (target.to_string(), value)
        })
        .collect()
}

#[derive(Debug, PartialEq, Eq)]
struct AddressLookupKey {
    variant: &'static str,
    key: [String; 8],
}

#[derive(Debug, PartialEq, Eq)]
enum AddressLookupDisposition {
    Continue,
    Match,
    OutOfCoverage,
}

fn address_lookup_disposition(
    outcome: &AddressOutcome,
    geocoder_build: &str,
) -> Result<AddressLookupDisposition> {
    match outcome {
        AddressOutcome::Resolved {
            data_version,
            candidates,
            ..
        } if data_version == geocoder_build => {
            if candidates.is_empty() {
                Ok(AddressLookupDisposition::Continue)
            } else {
                Ok(AddressLookupDisposition::Match)
            }
        }
        AddressOutcome::OutOfCoverage { data_version, .. } if data_version == geocoder_build => {
            Ok(AddressLookupDisposition::OutOfCoverage)
        }
        AddressOutcome::Resolved { .. } | AddressOutcome::OutOfCoverage { .. } => Err(
            Error::RustError("v2 address outcome differs from geocoder build".into()),
        ),
    }
}

/// Plan a bounded set of exact source-key representations for conventional
/// `state`/`region` + `city` input.
///
/// Overture sources commonly encode a city in either the last
/// `address_levels` value, `postal_city`, or both. The canonical fields expose
/// those source values literally, so an explicit canonical context always
/// remains a one-key exact lookup. Only the less-specific public aliases opt
/// into this compatibility bridge. A supplied `county` also keeps the exact
/// one-key interpretation because silently dropping it would weaken context.
fn address_lookup_plan(
    params: &HashMap<String, String>,
) -> std::result::Result<Vec<AddressLookupKey>, crate::address::ValidationError> {
    let exact = build_lookup_key(&address_params(params))?;
    let has_explicit_canonical_context =
        ["admin_level_general", "admin_level_specific", "postal_city"]
            .iter()
            .any(|name| params.contains_key(*name));
    let has_state_alias = ["state", "region"]
        .iter()
        .any(|name| params.contains_key(*name));
    let has_city_alias = params.contains_key("city");
    if has_explicit_canonical_context
        || !has_state_alias
        || !has_city_alias
        || params.contains_key("county")
        || exact[1].is_empty()
        || exact[3].is_empty()
    {
        return Ok(vec![AddressLookupKey {
            variant: "exact",
            key: exact,
        }]);
    }

    let city = exact[3].clone();
    let mut level_city = exact.clone();
    level_city[2] = city.clone();
    level_city[3].clear();
    let postal_city = exact.clone();
    let mut level_and_postal_city = exact;
    level_and_postal_city[2] = city;

    let candidates = [
        ("address_level_city", level_city),
        ("postal_city", postal_city),
        ("address_level_and_postal_city", level_and_postal_city),
    ];
    let mut plan = Vec::with_capacity(candidates.len());
    for (variant, key) in candidates {
        if !plan
            .iter()
            .any(|planned: &AddressLookupKey| planned.key == key)
        {
            plan.push(AddressLookupKey { variant, key });
        }
    }
    Ok(plan)
}

fn data_version_body(version: &DataVersion, features: Vec<Value>, metadata: Value) -> Value {
    json!({
        "type": "FeatureCollection",
        "features": features,
        "data_version": version,
        "metadata": metadata,
    })
}

fn division_feature(result: &geocoder_core::GeocoderResult) -> (f64, Value) {
    let score = (result.importance / 2.0).clamp(0.0, 1.0);
    (
        score,
        json!({
            "type": "Feature",
            "id": result.gers_id,
            "geometry": {"type": "Point", "coordinates": [result.lon, result.lat]},
            "bbox": result.bbox,
            "properties": {
                "name": result.primary_name,
                "feature_type": result.division_type,
                "relevance": score,
                "country": result.country,
                "region": result.region,
            },
        }),
    )
}

/// The proximity-aware division/POI seam.
///
/// The live defect: `q=McDonald's&proximity=<Times Square>` ranked McDonald
/// County, MO (relevance 0.5749) above every actual McDonald's (0.5199),
/// because the division lane ignores proximity entirely in the cross-lane
/// merge. When the user has stated a location, a division a continent away
/// with no distance relevance should not outrank an exact-name POI nearby.
///
/// This is deliberately narrow so the measured division/POI seam calibration
/// (t@1 0.261 -> 0.587) cannot move. The demotion fires only when all three
/// conditions hold:
///
/// - the request carries EXPLICIT proximity (never CF-IP inference, never
///   locality-inference centroids -- those set no `division_distance_km`);
/// - the division sits beyond [`DIVISION_PROXIMITY_RELEVANCE_KM`] of the bias
///   point (a user in a city is tens of km from its centroid; McDonald
///   County was 1,700 km away);
/// - the POI lane produced at least one exact-name match within that same
///   proximity-relevance radius. A global-head fall-through result elsewhere
///   on the planet must not demote a nearer division.
///
/// Every no-proximity query -- the entire calibrated seam -- takes the 1.0
/// path unchanged, and strong divisions survive the demotion anyway: a big
/// city at 0.84 demoted to ~0.71 still beats every non-prominent exact POI
/// (<= ~0.54), while a thin homonym county at 0.57 drops below it.
const DIVISION_PROXIMITY_RELEVANCE_KM: f64 = 100.0;
const DIVISION_PROXIMITY_DEMOTION: f64 = 0.85;

/// `poi_match_quality` at or above this counts as an exact-name POI for the
/// seam: 1.0 is byte-exact, 0.97 is the comma-truncated display of the same
/// exact name.
const EXACT_POI_NAME_QUALITY: f64 = 0.97;

/// The multiplier the division lane's score takes in the cross-lane merge.
fn division_proximity_demotion(
    division_distance_km: Option<f64>,
    nearest_exact_named_poi_distance_km: Option<f64>,
) -> f64 {
    match (division_distance_km, nearest_exact_named_poi_distance_km) {
        (Some(division_distance), Some(poi_distance))
            if division_distance > DIVISION_PROXIMITY_RELEVANCE_KM
                && poi_distance <= DIVISION_PROXIMITY_RELEVANCE_KM =>
        {
            DIVISION_PROXIMITY_DEMOTION
        }
        _ => 1.0,
    }
}

/// Ceiling on what a POI's static prior may contribute, before the shared /2.
///
/// Overture's per-place `confidence` is an EXISTENCE signal, not a prominence
/// one, and it saturates at 1.0 for an enormous number of records -- which is
/// why `q=Seattle` returned ten chain stores at relevance 1.0 with the city
/// absent entirely.
///
/// The value is derived, not chosen. The current places ladder is 1.0 exact,
/// 0.97 comma-qualified full name, 0.9 whole-word, then 0.8 prefix. The
/// binding semantic gap is 0.07, so confidence can order ties without
/// overturning that step: 0.5 * 0.08 = 0.04 < 0.07. It may cross the narrower
/// 0.03 exact/comma sub-rung, where other relevance is intentionally allowed
/// to order two renderings of the same full name.
///
/// The design note in `2026-07-31-search-quality-and-street-layer.md` proposed
/// "capped ~0.4"; that would contribute up to 0.2 and let a confident prefix
/// match beat an exact one. `the_poi_prior_cannot_outvote_a_match_quality_step`
/// pins the corrected relationship.
///
/// Divisions deliberately keep the wider 0.5 band, because their `importance`
/// IS a calibrated prominence signal (Wikidata, type prior, damped population)
/// and is supposed to separate Paris, France from Paris, Texas.
const POI_PRIOR_CAP: f64 = 0.08;

/// Distance decay band for explicit-proximity ranking, in pre-`/2` quality
/// units like `POI_PRIOR_CAP`'s `0.5 *` band.
///
/// The live defect this exists for: `q=starbucks&proximity=<Times Square>`
/// returned same-name, same-prior records at 67.0, 38.5, 52.3, 24.1 km in rank
/// order, because `distance_km` was computed and returned but never ranked on.
///
/// The budget follows the exact `POI_PRIOR_CAP` discipline. The current places
/// ladder is 1.0 exact, 0.97 comma-qualified full name, 0.9 whole-word, then
/// 0.8 prefix. Everything that is not text quality must stay under the binding
/// 0.07 semantic gap, so a nearer whole-word match cannot overturn a farther
/// comma-qualified full-name match. The budget deliberately exceeds the 0.03
/// exact/comma sub-rung, allowing proximity to choose a nearby qualified
/// rendering over a far exact rendering. When distance is known the confidence
/// prior shrinks by
/// `PROXIMITY_CONFIDENCE_SHRINK` (distance is a real relevance signal under
/// explicit proximity; confidence is an existence byte), and the two together
/// stay between those gaps:
/// `0.03 < 0.0375 + 0.5 * 0.25 * 0.08 = 0.0475 < 0.07`.
/// Within a rung, the distance band (0.0375) dominates the shrunken
/// confidence band (0.01), which is what makes "nearer wins" decisive among
/// near-equal text matches instead of an arbitrary tie.
const PROXIMITY_DISTANCE_BAND: f64 = 0.0375;
const PROXIMITY_CONFIDENCE_SHRINK: f64 = 0.25;

/// Half-value distance of the decay: a record this far away keeps half the
/// band. 2 km is "near me" scale -- city blocks matter, and by ~20 km the
/// bonus is nearly spent, which matches the proximity lane's own cell radius.
const PROXIMITY_HALF_DISTANCE_KM: f64 = 2.0;

/// Monotone decay in (0, 1]: 1 at the bias point, 1/2 at
/// `PROXIMITY_HALF_DISTANCE_KM`, asymptotically 0.
fn proximity_distance_decay(distance_km: f64) -> f64 {
    PROXIMITY_HALF_DISTANCE_KM / (PROXIMITY_HALF_DISTANCE_KM + distance_km.max(0.0))
}

fn primary_category_prior(category: &str) -> Option<f64> {
    Some(match category {
        "monument" => 1.0,
        "tourist_attraction" => 0.95,
        "airport" | "airport_terminal" => 0.90,
        "museum" | "history_museum" | "art_museum" | "castle" | "palace" => 0.85,
        "cathedral" => 0.80,
        "catholic_church" | "zoo" | "aquarium" => 0.60,
        "train_station"
        | "christian_place_of_worship"
        | "synagogue"
        | "mosque"
        | "temple"
        | "university" => 0.55,
        "subway_station" | "seaplane_bases" | "place_of_worship" | "art_gallery" => 0.50,
        "stadium_arena" | "opera_and_ballet" | "park" | "theatre" | "public_plaza" => 0.45,
        "library" | "hospital" => 0.40,
        "landmark_and_historical_building" | "historic_site" => 0.35,
        _ => return None,
    })
}

/// Correct a serialized prior using the primary category the Worker can
/// actually observe. Alternate categories are deliberately absent from the
/// serving projection; clamping prevents one of them from inflating an
/// unrelated primary such as `fountain` or `travel_services`.
fn effective_place_prominence(place: &PlaceProjection) -> f64 {
    let prominence = f64::from(place.prominence).clamp(0.0, 1.0);
    let category = place.category.trim();
    if category.is_empty() {
        return prominence;
    }
    if matches!(
        category,
        "landmark_and_historical_building" | "historic_site"
    ) {
        return prominence.max(0.85);
    }
    match primary_category_prior(category) {
        Some(maximum) => prominence.min(maximum),
        None => prominence.min(POI_PRIOR_CAP),
    }
}

/// Build the rank-time query for the places lane.
///
/// This is the ONLY constructor the places scoring path may use: retrieval
/// tokenization is NFKD-based (`places_pages::normalized_words`, mirroring the
/// `nfkd-lower-stripmark-cjk-bigram-v4` build tokenizer), so the rank-time
/// comparison must fold the query the same way or a record retrieval proved
/// (`skoda` matches the indexed token of "Škoda") scores `match_quality = 0`
/// and is floored by the quality gate. The fold runs once per query here, not
/// once per candidate.
fn poi_normalized_query(query: &str) -> NormalizedQuery {
    NormalizedQuery::new(&fold_for_scoring(query))
}

fn poi_match_quality(primary_name: &str, query: &NormalizedQuery) -> f64 {
    // Fold the candidate name with the SAME NFKD scoring fold the query got
    // in `poi_normalized_query` (once per candidate, reused by both the
    // ladder and the token comparison below). The core ladder's own
    // `normalize_for_match` then runs on already-folded text, where it is a
    // no-op.
    let folded_name = fold_for_scoring(primary_name);
    let quality = geocoder_core::query::match_quality(&folded_name, query);
    let query_tokens = query_terms(query.as_str());
    let name_tokens = query_terms(&folded_name);

    // The shared ladder intentionally comma-truncates display names. Preserve
    // exact full-name equality at 1.0 while putting comma-qualified names on a
    // distinct rung so UUID order cannot decide the result.
    if quality == 1.0 && name_tokens != query_tokens {
        return 0.97;
    }

    // A character LCP rewards a longer name merely for sharing the start of a
    // multi-token query. Replace only that partial rung with bounded token
    // coverage and name coverage; exact and prefix rungs remain untouched.
    if query_tokens.len() > 1 && quality > 0.0 && quality < 0.8 {
        let name_set: HashSet<&str> = name_tokens.iter().map(String::as_str).collect();
        let present = query_tokens
            .iter()
            .filter(|token| name_set.contains(token.as_str()))
            .collect::<Vec<_>>();
        let name_chars: usize = name_tokens.iter().map(|token| token.chars().count()).sum();
        if name_chars == 0 {
            return 0.0;
        }
        let matched_chars: usize = present.iter().map(|token| token.chars().count()).sum();
        return 0.7 * present.len() as f64 / query_tokens.len() as f64 * matched_chars as f64
            / name_chars as f64;
    }
    quality
}

/// Score a POI on the SAME composition divisions already use:
/// `(match_quality + 0.5 * static_prior) / 2`, clamped.
///
/// Without a text query -- reverse, or a proximity-only lookup -- there is
/// nothing to match against, so the previous confidence score stands.
fn place_score(place: &PlaceProjection, query: Option<&NormalizedQuery>) -> f64 {
    let confidence = f64::from(place.confidence).clamp(0.0, 1.0);
    let prominence = effective_place_prominence(place);
    match query {
        Some(query) => {
            let quality = poi_match_quality(&place.name, query);
            // A real prominence prior gets the SAME 0.5 static band divisions
            // get, because it is the same kind of signal -- a calibrated
            // category prior. POI_PRIOR_CAP exists only to defend against
            // `confidence`, which is an existence byte; it stays in force
            // wherever no prominence is available (PCSH pages, PLHD0002
            // shards), so this reproduces the measured fix-2 behaviour there
            // exactly rather than silently re-ranking already-published data.
            //
            // `distance_km` is present exactly when a proximity point routed
            // the lookup. There it joins the sub-rung band: the confidence
            // prior shrinks so distance dominates it, while calibrated
            // prominence keeps its full band -- fame still separates the
            // Casino de Monte-Carlo from a nearby snack bar, but among
            // near-equal text matches the nearer record now wins
            // deterministically instead of by producer order.
            let (prior, distance_component) = match place.distance_km {
                Some(distance) => {
                    let prior = if prominence > 0.0 {
                        prominence
                    } else {
                        PROXIMITY_CONFIDENCE_SHRINK * POI_PRIOR_CAP * confidence
                    };
                    (
                        prior,
                        PROXIMITY_DISTANCE_BAND * proximity_distance_decay(distance),
                    )
                }
                None => {
                    let prior = if prominence > 0.0 {
                        prominence
                    } else {
                        POI_PRIOR_CAP * confidence
                    };
                    (prior, 0.0)
                }
            };
            ((quality + 0.5 * prior + distance_component) / 2.0).clamp(0.0, 1.0)
        }
        None => confidence,
    }
}

fn place_feature(place: &PlaceProjection, query: Option<&NormalizedQuery>) -> (f64, Value) {
    let score = place_score(place, query);
    (
        score,
        json!({
            "type": "Feature",
            "id": place.id,
            "geometry": {"type": "Point", "coordinates": [place.longitude, place.latitude]},
            "properties": {
                "name": place.name,
                "feature_type": "poi",
                "category": place.category,
                "locality": place.locality,
                "region": place.region,
                "country": place.country,
                "relevance": score,
                "distance_km": place.distance_km,
            },
        }),
    )
}

#[derive(Debug, Clone, PartialEq)]
struct LocalitySuffixCandidate {
    place_query: String,
    locality_query: String,
    locality_tokens: Vec<String>,
}

#[derive(Debug, Clone, PartialEq)]
struct PlacesLocalityInference {
    locality_query: String,
    division_id: String,
    division_type: String,
    longitude: f64,
    latitude: f64,
}

fn locality_suffix_candidates(
    tokens: &[String],
    explicit_proximity: bool,
    global_head: &[PlaceProjection],
) -> Vec<LocalitySuffixCandidate> {
    // Two tokens are included (RC6): `IKEA Berlin` returned nothing at all,
    // because two tokens got no routing and then died intersecting two ten-deep
    // head lists.
    //
    // For two-token queries a non-empty head remains authoritative: this
    // preserves the original "fallback cannot displace a real answer" rule.
    // RC3 means a three-token name+locality query can now return a non-empty
    // global head before locality inference. Permit the fallback in that case
    // only when the head itself contains an exact name for the prefix -- e.g.
    // `Taj Mahal` in `Taj Mahal Agra`. `Statue of Liberty` has no exact
    // `Statue of` prefix result, so Liberty, NY cannot steal that landmark.
    if explicit_proximity
        || !(2..=6).contains(&tokens.len())
        || (!global_head.is_empty() && tokens.len() == 2)
    {
        return Vec::new();
    }
    let mut candidates = (1..=2)
        .rev()
        .filter(|suffix_length| *suffix_length < tokens.len())
        .map(|suffix_length| {
            let split = tokens.len() - suffix_length;
            let locality_tokens = tokens[split..].to_vec();
            LocalitySuffixCandidate {
                place_query: tokens[..split].join(" "),
                locality_query: locality_tokens.join(" "),
                locality_tokens,
            }
        })
        .collect::<Vec<_>>();
    if !global_head.is_empty() {
        candidates.retain(|candidate| {
            let place_tokens = query_terms(&candidate.place_query);
            global_head.iter().any(|place| {
                query_terms(place.name.split(',').next().unwrap_or("")) == place_tokens
            })
        });
    }
    candidates
}

/// Does one of the row's *alternate* searchable names equal the suffix
/// exactly? `search_name` is the `;`-separated primary + alternates/exonyms
/// column already carried by every division row (new shards; `None` on legacy
/// shards, where the fallback is simply unavailable). This is what lets
/// `Tokyo` resolve 東京都 and `Mexico City` resolve Ciudad de México -- the
/// endonym is the primary name, so the exact primary-name rung can never fire
/// for those.
fn alternate_locality_name_matches(
    result: &geocoder_core::GeocoderResult,
    locality_tokens: &[String],
) -> bool {
    result.search_name.as_deref().is_some_and(|names| {
        names
            .split(';')
            .any(|segment| query_terms(segment.split(',').next().unwrap_or("")) == locality_tokens)
    })
}

fn exact_primary_locality_name_matches(
    result: &geocoder_core::GeocoderResult,
    locality_tokens: &[String],
) -> bool {
    query_terms(result.primary_name.split(',').next().unwrap_or("")) == locality_tokens
}

/// The bounded set of localities a suffix may resolve to, best first.
///
/// Two properties this ordering has to keep:
/// - Exact primary-name matches come first, in the division lane's own
///   (importance) order, so the *first* attempt is byte-for-byte the locality
///   the previous single-shot `exact_locality_result` picked. Nothing that
///   works today can move.
/// - Alternate-name matches are appended after them, so the alt-name rung is
///   purely additive: it can only supply localities where the exact rung
///   found none, or extra retries after the exact ones came back empty.
///
/// Capped at [`LOCALITY_INFERENCE_ATTEMPT_CAP`]: each entry costs one routed
/// places search.
fn exact_locality_results<'a>(
    candidate: &LocalitySuffixCandidate,
    results: &'a [geocoder_core::GeocoderResult],
) -> Vec<&'a geocoder_core::GeocoderResult> {
    let mut primary = Vec::new();
    let mut alternate = Vec::new();
    for result in results {
        if !LOCALITY_SUFFIX_TYPES.contains(&normalized_type(&result.division_type).as_str()) {
            continue;
        }
        if exact_primary_locality_name_matches(result, &candidate.locality_tokens) {
            primary.push(result);
        } else if alternate_locality_name_matches(result, &candidate.locality_tokens) {
            alternate.push(result);
        }
        if primary.len() >= LOCALITY_INFERENCE_ATTEMPT_CAP {
            break;
        }
    }
    primary.extend(alternate);
    primary.truncate(LOCALITY_INFERENCE_ATTEMPT_CAP);
    primary
}

/// Should this routed attempt's result be adopted, and should the retry loop
/// stop? Pure so the policy is testable without a loader.
///
/// - A non-empty routed result is always adopted and always stops the loop.
/// - An empty one is adopted only on the first attempt, which reproduces the
///   pre-retry behaviour exactly (inference fired, routed lane empty, metadata
///   still records the inference) while later empty attempts leave that first
///   adoption in place.
fn locality_attempt_disposition(attempt_index: usize, routed_is_empty: bool) -> (bool, bool) {
    (!routed_is_empty || attempt_index == 0, !routed_is_empty)
}

fn places_locality_inference(
    candidate: &LocalitySuffixCandidate,
    result: &geocoder_core::GeocoderResult,
) -> (String, PlacesLocalityInference) {
    (
        candidate.place_query.clone(),
        PlacesLocalityInference {
            locality_query: candidate.locality_query.clone(),
            division_id: result.gers_id.clone(),
            division_type: normalized_type(&result.division_type),
            longitude: result.lon,
            latitude: result.lat,
        },
    )
}

/// Bounded homonym-tolerant retry: at most this many localities are tried for
/// one query, so the worst case is three routed places searches instead of one.
const LOCALITY_INFERENCE_ATTEMPT_CAP: usize = 3;

/// The ordered, bounded routing plans for a query's locality suffix. Empty
/// when no suffix resolves; otherwise the first entry is the locality the
/// single-shot predecessor would have chosen.
async fn infer_places_locality(
    loader: &ShardLoader,
    core_version: &str,
    candidates: &[LocalitySuffixCandidate],
) -> Result<Vec<(String, PlacesLocalityInference)>> {
    let allowed_types: HashSet<String> = LOCALITY_SUFFIX_TYPES
        .iter()
        .map(|value| (*value).to_string())
        .collect();
    for candidate in candidates {
        let query = GeocoderQuery::new(&candidate.locality_query)
            .with_limit(MAX_RESULTS)
            .with_autocomplete(false)
            .with_allowed_types(Some(allowed_types.clone()));
        let search = loader
            .search_version(core_version, &query, &UserLocation::default(), false)
            .await?;
        let matches = exact_locality_results(candidate, &search.results);
        if !matches.is_empty() {
            return Ok(matches
                .into_iter()
                .map(|result| places_locality_inference(candidate, result))
                .collect());
        }
    }
    Ok(Vec::new())
}

fn apply_places_locality_inference(
    places: &mut [PlaceProjection],
    inference: &PlacesLocalityInference,
) -> Value {
    // The centroid is an inferred routing point, not user proximity. Do not
    // expose distance from it through the public user-distance field.
    for place in places {
        place.distance_km = None;
    }
    json!({
        "query": inference.locality_query,
        "division_id": inference.division_id,
        "division_type": inference.division_type,
        "routing": "locality_centroid",
    })
}

fn text_metadata(
    types: &HashSet<String>,
    proximity: Option<(f64, f64)>,
    places_locality_inference: Option<Value>,
    places_prefix_head_fallback: Option<Value>,
) -> Value {
    let mut metadata = json!({
        "mode": "text",
        "types": types,
        "proximity": proximity,
    });
    if let Some(inference) = places_locality_inference {
        metadata["places_locality_inference"] = inference;
    }
    if let Some(fallback) = places_prefix_head_fallback {
        metadata["places_prefix_head_fallback"] = fallback;
    }
    metadata
}

/// Describe a fired prefix-head fallback: which tokens were probed and which
/// were proven from stored display fields rather than from a posting.
fn prefix_head_fallback_metadata(tokens: &[String]) -> Option<Value> {
    let (head_tokens, dropped) = prefix_head_fallback_split(tokens)?;
    Some(json!({
        "probe_query": head_tokens.join(" "),
        "verified_tokens": dropped,
        "verification": "display_fields",
    }))
}

fn reverse_hit_feature(hit: ReverseHit) -> Value {
    let record = hit.record;
    match record.family {
        ReverseFamily::Places => json!({
            "type": "Feature",
            "id": record.id,
            "geometry": {
                "type": "Point",
                "coordinates": [record.longitude, record.latitude],
            },
            "properties": {
                "feature_type": "poi",
                "name": record.primary_name,
                "brand": record.brand_name,
                "category": record.category,
                "locality": record.locality,
                "region": record.region,
                "country": record.country,
                "confidence_rank": record.confidence_rank,
                "distance_m": hit.distance_m,
            },
        }),
        ReverseFamily::Addresses => json!({
            "type": "Feature",
            "id": record.id,
            "geometry": {
                "type": "Point",
                "coordinates": [record.longitude, record.latitude],
            },
            "properties": {
                "feature_type": "address",
                "street": record.street,
                "number": record.number,
                "unit": record.unit,
                "postcode": record.postcode,
                "postal_city": record.postal_city,
                "address_levels": record.address_levels,
                "display_country": record.display_country,
                "distance_m": hit.distance_m,
            },
        }),
    }
}

fn haversine_km(latitude_a: f64, longitude_a: f64, latitude_b: f64, longitude_b: f64) -> f64 {
    let radius_km = 6371.0088_f64;
    let delta_latitude = (latitude_b - latitude_a).to_radians();
    let delta_longitude = (longitude_b - longitude_a).to_radians();
    let latitude_a = latitude_a.to_radians();
    let latitude_b = latitude_b.to_radians();
    let haversine = (delta_latitude / 2.0).sin().powi(2)
        + latitude_a.cos() * latitude_b.cos() * (delta_longitude / 2.0).sin().powi(2);
    2.0 * radius_km * haversine.sqrt().asin()
}

/// Serve `/v2/forward` Places from a promoted construction slice
/// (`PLRV0002+PLHD0002`). The no-proximity lane resolves each exact token
/// through the sharded global head; the proximity lane derives the level-4
/// construction cell for the bias point, selects each token's nibble-prefix
/// subpartition from routing.json, and answers from the routed `.plrv`
/// artifact. Both artifact classes carry exact tokens only, so autocomplete
/// prefix expansion does not apply here.
async fn search_places_construction(
    loader: &ShardLoader,
    entrypoint: &ArtifactIdentity,
    query: &str,
    proximity: Option<(f64, f64)>,
    proximity_is_explicit: bool,
) -> Result<Vec<PlaceProjection>> {
    const SUFFIX: &str = "/routing.json";
    let object_root = entrypoint
        .object_key
        .strip_suffix(SUFFIX)
        .ok_or_else(|| Error::RustError("v2 Places entrypoint is not a routing object".into()))?;
    let tokens = query_terms(query);
    if tokens.is_empty() || tokens.len() > ROUTED_QUERY_TOKEN_CAP {
        return Ok(Vec::new());
    }
    let routing = loader
        .lookup_places_construction_routing(&entrypoint.object_key)
        .await?;
    if let Some((longitude, latitude)) = proximity {
        let mut records = Vec::new();
        if let Some(cell) = construction_cell(longitude, latitude) {
            records =
                places_construction_routed_records(loader, object_root, &routing, &cell, &tokens)
                    .await?;
            // Additive CJK recovery, on an otherwise-empty routed answer only. An
            // unsegmented CJK query is one word, and the index holds that whole run
            // only for a record named exactly it; the run's bigrams ARE indexed for
            // longer names. Re-probe with them, then keep only candidates whose own
            // stored name carries the run. Bounded by the same four-clause cap, so
            // this costs at most one extra routed plan and four token reads, and it
            // can never displace a result the exact clauses produced.
            if records.is_empty() {
                if let Some(expansion) = cjk_query_expansion(query, ROUTED_QUERY_TOKEN_CAP) {
                    let candidates = places_construction_routed_records(
                        loader,
                        object_root,
                        &routing,
                        &cell,
                        &expansion,
                    )
                    .await?;
                    records = retain_records_proving_query_run(candidates, query);
                }
            }
            // Neighbor-cell probe: a bias point near a cell edge (or in a cell
            // that answered nothing) also asks the adjacent cell(s). Bounded at
            // two extra cells, chosen by proximity to the edges; records are
            // cell-partitioned so the union cannot double-count, but identity
            // dedup below keeps that a checked property rather than a hope.
            // For a head-servable query an empty primary routed cell will fall
            // through to the global head, so do not first spend two
            // unconditional nearest-neighbor probes. Four-token queries cannot
            // use the head and retain that bounded rescue path. Ordinary
            // edge-neighbor probes remain active for every token count.
            let expand_empty_primary = records.is_empty() && tokens.len() > HEAD_QUERY_TOKEN_CAP;
            for neighbor in neighbor_construction_cells(longitude, latitude, expand_empty_primary) {
                let extra = places_construction_routed_records(
                    loader,
                    object_root,
                    &routing,
                    &neighbor,
                    &tokens,
                )
                .await?;
                records.extend(extra);
            }
            let mut seen = HashSet::new();
            records.retain(|record| {
                seen.insert((
                    record.id.clone(),
                    record.source_object_index,
                    record.source_row_group,
                    record.source_row_index,
                ))
            });
        }
        // Empty-cell fall-through: explicit proximity biases, it must not
        // veto. When the routed lane (primary cell, CJK recovery, and neighbor
        // probes together) yields nothing, answer from the global head exactly
        // as the no-proximity lane would, with distances attached so the
        // distance term still orders what comes back. A proximity query for a
        // distant landmark now answers instead of returning empty.
        if routed_lane_falls_through_to_head(
            proximity_is_explicit,
            records.is_empty(),
            tokens.len(),
        ) {
            records =
                places_construction_head_records(loader, object_root, &routing, &tokens).await?;
        }
        let mut results: Vec<PlaceProjection> = records.iter().map(record_projection).collect();
        for place in &mut results {
            place.distance_km = Some(haversine_km(
                latitude,
                longitude,
                f64::from(place.latitude),
                f64::from(place.longitude),
            ));
        }
        return Ok(results);
    }
    if tokens.len() > HEAD_QUERY_TOKEN_CAP {
        return Ok(Vec::new());
    }
    let records = places_construction_head_records(loader, object_root, &routing, &tokens).await?;
    Ok(records.iter().map(record_projection).collect())
}

/// Whether an empty routed (proximity) answer may be served by the global
/// head instead. EXPLICIT proximity is a bias, not a hard filter: it must never
/// turn a query the no-proximity lane could answer into an empty response. The
/// head keeps its own token cap; wider queries continue to the additive
/// prefix-head last resort in `handle_forward`.
///
/// An INFERRED locality centroid (`proximity_is_explicit` false) is excluded,
/// and that exclusion is load-bearing rather than cosmetic. The centroid lane
/// is a routing decision, not a user statement, and `handle_forward`'s bounded
/// homonym retry reads the routed answer's emptiness as its signal to try the
/// next locality: falling an empty Rochester, MN through to the global head
/// would make attempt 0 non-empty, kill the retry, and label a global head
/// answer as `routing: locality_centroid` for the wrong division.
fn routed_lane_falls_through_to_head(
    proximity_is_explicit: bool,
    routed_is_empty: bool,
    token_count: usize,
) -> bool {
    proximity_is_explicit && routed_is_empty && (1..=HEAD_QUERY_TOKEN_CAP).contains(&token_count)
}

/// Resolve one routed (proximity) clause set inside `cell`.
///
/// A bias point in an unpopulated cell is an empty result, not an error; a
/// populated cell that owns no subpartition for a token hash is a broken tiling
/// invariant and fails closed inside the plan.
async fn places_construction_routed_records(
    loader: &ShardLoader,
    object_root: &str,
    routing: &PlacesRouting,
    cell: &str,
    tokens: &[String],
) -> Result<Vec<crate::places_construction_v1::PlacesV1Record>> {
    if tokens.is_empty() || tokens.len() > ROUTED_QUERY_TOKEN_CAP {
        return Ok(Vec::new());
    }
    let Some(plan) = routed_fetch_plan(routing, cell, tokens).map_err(Error::RustError)? else {
        return Ok(Vec::new());
    };
    // Aggregate residency bound: the plan groups tokens by owning object. Each
    // object is range-read through its fixed index and one bounded payload at a
    // time; a 209 MiB planet object is never materialized in the 128 MiB
    // isolate. The edge cache absorbs cross-request refetches.
    let mut per_token: Vec<Option<Vec<crate::places_construction_v1::PlacesV1Record>>> =
        (0..tokens.len()).map(|_| None).collect();
    for (object, token_indexes) in plan {
        let object_key = format!("{object_root}/objects/{object}");
        let object_tokens: Vec<String> = token_indexes
            .iter()
            .map(|index| tokens[*index].clone())
            .collect();
        let object_records = loader
            .lookup_places_construction_routed(&object_key, cell, &object_tokens)
            .await?;
        for (index, records) in token_indexes.into_iter().zip(object_records) {
            if records.is_empty() {
                return Ok(Vec::new());
            }
            per_token[index] = Some(records);
        }
    }
    let per_token = per_token
        .into_iter()
        .map(|records| {
            records.ok_or_else(|| {
                Error::RustError("v2 Places routed fetch plan missed a token".into())
            })
        })
        .collect::<Result<Vec<_>>>()?;
    merge_routed_candidates(tokens, per_token).map_err(Error::RustError)
}

/// Resolve up to `HEAD_QUERY_TOKEN_CAP` exact tokens through the sharded global
/// head, composing the entity-phrase lane with the ordinary token lane. At most
/// three ordinary reads plus two phrase reads per call.
async fn places_construction_head_records(
    loader: &ShardLoader,
    object_root: &str,
    routing: &PlacesRouting,
    tokens: &[String],
) -> Result<Vec<crate::places_construction_v1::PlacesV1Record>> {
    if tokens.is_empty() || tokens.len() > HEAD_QUERY_TOKEN_CAP {
        return Ok(Vec::new());
    }
    let head_manifest_key = format!("{object_root}/objects/{}", routing.head.manifest_object);
    let head = loader
        .lookup_places_construction_head_routing(&head_manifest_key, &routing.head)
        .await?;
    let mut phrase_groups = Vec::new();
    if head.admits_entity_phrases() {
        for phrase_tokens in entity_phrase_token_groups(tokens) {
            let mut phrase_records = Vec::new();
            if let Some(phrase_key) = entity_phrase_key(phrase_tokens) {
                let shard_id = head_shard_id(&phrase_key, head.shard_bits);
                if let Some(shard) = head.shard(shard_id) {
                    let object_key = format!("{object_root}/objects/{}", shard.path);
                    let bytes = loader
                        .places_construction_object(&object_key, MAX_HEAD_SHARD_BYTES)
                        .await?;
                    let records = head_shard_lookup(&bytes, shard_id, head.shard_bits, &phrase_key)
                        .map_err(Error::RustError)?;
                    phrase_records = validate_entity_phrase_records(phrase_tokens, records);
                }
            }
            // Keep empty groups so index zero always denotes the full query;
            // composition may apply its saturated-full-phrase recovery rule.
            phrase_groups.push(phrase_records);
        }
    }
    let mut per_token = Vec::with_capacity(tokens.len());
    let mut ordinary_complete = true;
    for token in tokens {
        let shard_id = head_shard_id(token, head.shard_bits);
        // An unpopulated shard means no head record exists for the token.
        let Some(shard) = head.shard(shard_id) else {
            ordinary_complete = false;
            break;
        };
        let object_key = format!("{object_root}/objects/{}", shard.path);
        let bytes = loader
            .places_construction_object(&object_key, MAX_HEAD_SHARD_BYTES)
            .await?;
        let records = head_shard_lookup(&bytes, shard_id, head.shard_bits, token)
            .map_err(Error::RustError)?;
        if records.is_empty() {
            ordinary_complete = false;
            break;
        }
        per_token.push(records);
    }
    let ordinary = if ordinary_complete {
        merge_head_candidates(tokens, per_token).map_err(Error::RustError)?
    } else {
        Vec::new()
    };
    compose_entity_phrase_candidates(phrase_groups, ordinary).map_err(Error::RustError)
}

/// Additive prefix-head fallback for 4-6-token no-proximity queries.
///
/// `HEAD_QUERY_TOKEN_CAP` empties every wider no-proximity query before any
/// index read, which is why "GEYLANG BAHRU MRT STATION" returns nothing while
/// "YISHUN MRT STATION" hits. Probe the head ONCE with the first three tokens
/// (their `e2:`/`e3:` phrase keys included, so an exact two- or three-word name
/// prefix still carries its phrase evidence), then fail-closed verify the
/// dropped tail against each candidate's stored display fields.
///
/// This lane is additive only: `handle_text` runs it exclusively on an
/// otherwise-empty response, so it can never displace, reorder, or regress a
/// result the ordinary lanes produced. It adds at most one head-manifest lookup
/// and five head shard reads.
async fn search_places_prefix_head_fallback(
    loader: &ShardLoader,
    family: &FamilyReference,
    query: &str,
    proximity: Option<(f64, f64)>,
) -> Result<Vec<PlaceProjection>> {
    if !supports_places_construction_format(&family.versions.format) {
        return Ok(Vec::new());
    }
    let Some(entrypoint) = family.entrypoints.get("forward") else {
        return Ok(Vec::new());
    };
    const SUFFIX: &str = "/routing.json";
    let Some(object_root) = entrypoint.object_key.strip_suffix(SUFFIX) else {
        return Ok(Vec::new());
    };
    let tokens = query_terms(query);
    let Some((head_tokens, dropped)) = prefix_head_fallback_split(&tokens) else {
        return Ok(Vec::new());
    };
    let routing = loader
        .lookup_places_construction_routing(&entrypoint.object_key)
        .await?;
    let records =
        places_construction_head_records(loader, object_root, &routing, head_tokens).await?;
    let verified = retain_records_proving_dropped_tokens(records, dropped);
    let mut results: Vec<PlaceProjection> = verified.iter().map(record_projection).collect();
    // When explicit proximity reaches this last resort (its routed lane came
    // back empty), attach distances so the distance term orders the answer.
    if let Some((longitude, latitude)) = proximity {
        for place in &mut results {
            place.distance_km = Some(haversine_km(
                latitude,
                longitude,
                f64::from(place.latitude),
                f64::from(place.longitude),
            ));
        }
    }
    Ok(results)
}

async fn search_places(
    loader: &ShardLoader,
    family: &FamilyReference,
    query: &str,
    proximity: Option<(f64, f64)>,
    // False when `proximity` is an inferred locality centroid rather than a
    // point the request stated. Only the head fall-through reads it; see
    // `routed_lane_falls_through_to_head`.
    proximity_is_explicit: bool,
    autocomplete: bool,
    limit: usize,
) -> Result<Vec<PlaceProjection>> {
    let entrypoint = family
        .entrypoints
        .get("forward")
        .ok_or_else(|| Error::RustError("v2 Places family omits its forward entrypoint".into()))?;
    if supports_places_construction_format(&family.versions.format) {
        return search_places_construction(
            loader,
            entrypoint,
            query,
            proximity,
            proximity_is_explicit,
        )
        .await;
    }
    const SUFFIX: &str = "/catalog.pcat";
    let object_root = entrypoint
        .object_key
        .strip_suffix(SUFFIX)
        .ok_or_else(|| Error::RustError("v2 Places entrypoint is not a catalog object".into()))?;
    let tokens = query_terms(query);
    if tokens.is_empty() || tokens.len() > 4 {
        return Ok(Vec::new());
    }
    if let Some((longitude, latitude)) = proximity {
        let prefix_last = autocomplete
            && tokens
                .last()
                .is_some_and(|token| token.chars().count() >= 2)
            && !query.chars().last().is_some_and(char::is_whitespace);
        let token_count = tokens.len();
        let clauses = tokens
            .into_iter()
            .enumerate()
            .map(|(index, token)| {
                PlacesClause::new(token, prefix_last && index + 1 == token_count, None)
            })
            .collect::<Result<Vec<_>>>()?;
        let catalog = loader.lookup_places_catalog(&entrypoint.object_key).await?;
        let Some(route) = catalog.route_point(longitude, latitude).cloned() else {
            return Ok(Vec::new());
        };
        let mut lookup = loader
            .lookup_places_shard(&format!("{object_root}/{}", route.object), &clauses)
            .await?;
        for place in &mut lookup.results {
            place.distance_km = Some(haversine_km(
                latitude,
                longitude,
                f64::from(place.latitude),
                f64::from(place.longitude),
            ));
        }
        lookup.results.truncate(limit);
        return Ok(lookup.results);
    }
    if tokens.len() > HEAD_QUERY_TOKEN_CAP {
        return Ok(Vec::new());
    }
    let clauses = tokens
        .into_iter()
        .map(|token| PlacesClause::new(token, false, None))
        .collect::<Result<Vec<_>>>()?;
    let mut lookup = loader
        .lookup_places_head(&format!("{object_root}/head.phrp"), &clauses)
        .await?;
    lookup.results.truncate(limit);
    Ok(lookup.results)
}

pub(crate) async fn handle_forward(
    req: Request,
    ctx: RouteContext<std::rc::Rc<Context>>,
) -> Result<Response> {
    let params: HashMap<String, String> = req
        .url()?
        .query_pairs()
        .map(|(key, value)| (key.into_owned(), value.into_owned()))
        .collect();
    let types = match parse_types(params.get("types")) {
        Ok(value) => value,
        Err(message) => return json_error("invalid_request", &message, 400),
    };
    let loader = ShardLoader::with_context(&ctx.env, ctx.data.clone())?;
    let release = match load_available_release(&loader).await? {
        ReleaseAvailability::Ready(release) => release,
        ReleaseAvailability::Unavailable(response) => return Ok(response),
    };

    if has_structured_address(&params) {
        if params.contains_key("q") {
            return json_error(
                "invalid_request",
                "q cannot be combined with structured address fields",
                400,
            );
        }
        if params.contains_key("types") && (types.len() != 1 || !types.contains("address")) {
            return json_error(
                "invalid_request",
                "structured address lookup requires types=address when types is supplied",
                400,
            );
        }
        if ["limit", "autocomplete", "proximity"]
            .iter()
            .any(|parameter| params.contains_key(*parameter))
        {
            return json_error(
                "invalid_request",
                "limit, autocomplete, and proximity do not apply to structured exact lookup",
                400,
            );
        }
        let lookup_plan = match address_lookup_plan(&params) {
            Ok(value) => value,
            Err(error) => return json_error("invalid_request", &error.message(), 400),
        };
        let family = release
            .families
            .get("addresses")
            .filter(|family| family.entrypoints.contains_key("structured_forward"));
        let Some(family) = family else {
            return json_error(
                "capability_unavailable",
                "structured address data is unavailable",
                503,
            );
        };
        let entrypoint = &family.entrypoints["structured_forward"];
        let mut lookup_attempts = 0;
        let mut resolution_variant = "none";
        let mut final_outcome = None;
        for planned in &lookup_plan {
            lookup_attempts += 1;
            let outcome = if family.versions.format == ADDRESS_CONSTRUCTION_FORMAT {
                loader
                    .lookup_address_construction(
                        &planned.key,
                        &entrypoint.object_key,
                        &release.geocoder_build,
                    )
                    .await?
            } else {
                loader
                    .lookup_address_entrypoint(
                        &planned.key,
                        &entrypoint.object_key,
                        &release.geocoder_build,
                    )
                    .await?
            };
            let disposition = address_lookup_disposition(&outcome, &release.geocoder_build)?;
            if disposition == AddressLookupDisposition::Match {
                resolution_variant = planned.variant;
            }
            final_outcome = Some(outcome);
            if disposition != AddressLookupDisposition::Continue {
                break;
            }
        }
        let outcome = final_outcome
            .ok_or_else(|| Error::RustError("v2 address lookup plan unexpectedly empty".into()))?;
        let (coverage, normalization_version, records) = match outcome {
            AddressOutcome::Resolved {
                data_version,
                normalization_version,
                candidates,
            } if data_version == release.geocoder_build => {
                ("in_coverage", normalization_version, candidates)
            }
            AddressOutcome::OutOfCoverage {
                data_version,
                normalization_version,
            } if data_version == release.geocoder_build => {
                ("out_of_coverage", normalization_version, Vec::new())
            }
            AddressOutcome::Resolved { .. } | AddressOutcome::OutOfCoverage { .. } => {
                return Err(Error::RustError(
                    "v2 address outcome differs from geocoder build".into(),
                ));
            }
        };
        if records.len() > ADDRESS_CANDIDATE_CAP {
            return json_error(
                "candidate_overflow",
                "structured address candidate cap exceeded",
                413,
            );
        }
        let features: Vec<Value> = records
            .into_iter()
            .map(|record| {
                json!({
                    "type": "Feature",
                    "id": record.id,
                    "geometry": {"type": "Point", "coordinates": [record.longitude, record.latitude]},
                    "properties": {
                        "name": format!("{} {}", record.number, record.street).trim().to_string(),
                        "feature_type": "address",
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
                    },
                })
            })
            .collect();
        let candidate_count = features.len();
        let body = data_version_body(
            &release.data_version,
            features,
            json!({
                "mode": "structured_address",
                "coverage": coverage,
                "normalization_version": normalization_version,
                "resolution_variant": resolution_variant,
                "lookup_attempts": lookup_attempts,
                "candidate_count": candidate_count,
                "ambiguous": candidate_count > 1,
            }),
        );
        return versioned_response(&body, &release.data_version, 200);
    }

    let Some(query) = params.get("q") else {
        return json_error(
            "invalid_request",
            "q or structured address fields are required",
            400,
        );
    };
    if query.is_empty() || query.len() > MAX_QUERY_BYTES {
        return json_error("invalid_request", "q is empty or too long", 400);
    }
    if types.contains("address") {
        return json_error(
            "capability_unavailable",
            "free-text address search is not available; supply structured street, number, and country fields",
            400,
        );
    }
    let limit = match parse_limit(params.get("limit")) {
        Ok(value) => value,
        Err(message) => return json_error("invalid_request", &message, 400),
    };
    let autocomplete = match parse_bool(params.get("autocomplete"), true) {
        Ok(value) => value,
        Err(message) => return json_error("invalid_request", &message, 400),
    };
    let proximity = match parse_proximity(&params) {
        Ok(value) => value,
        Err(message) => return json_error("invalid_request", &message, 400),
    };
    let division_types: HashSet<_> = types
        .iter()
        .filter(|value| DIVISION_TYPES.contains(&value.as_str()))
        .cloned()
        .collect();
    // Division candidates carry their distance from the EXPLICIT proximity
    // point (None without one) so the proximity-aware seam below can weigh
    // them against exact-name POIs before the cross-lane merge.
    let mut division_candidates: Vec<(f64, Value, Option<f64>)> = Vec::new();
    if !division_types.is_empty() {
        let mut user_location = UserLocation::from_request(&req);
        if let Some((longitude, latitude)) = proximity {
            user_location.lon = Some(longitude);
            user_location.lat = Some(latitude);
        }
        if let Some(country) = params.get("country") {
            user_location.country = Some(country.to_uppercase());
        }
        let bias = match (&user_location.country, user_location.lat, user_location.lon) {
            (Some(country), Some(lat), Some(lon)) => LocationBias::Full {
                country: country.clone(),
                lat,
                lon,
            },
            (Some(country), None, None) => LocationBias::Country(country.clone()),
            (None, Some(lat), Some(lon)) => LocationBias::Coordinates { lat, lon },
            _ => LocationBias::None,
        };
        let query = GeocoderQuery::new(query)
            .with_limit(limit)
            .with_autocomplete(autocomplete)
            .with_bias(bias)
            .with_allowed_types(Some(division_types));
        let search = loader
            .search_version(release.core_version(), &query, &user_location, false)
            .await?;
        division_candidates.extend(search.results.iter().map(|result| {
            let (score, feature) = division_feature(result);
            let distance_km = proximity.map(|(longitude, latitude)| {
                haversine_km(latitude, longitude, result.lat, result.lon)
            });
            (score, feature, distance_km)
        }));
    }
    let mut places_locality_inference = None;
    let mut places_prefix_head_fallback = None;
    let mut poi_ranked: Vec<(f64, Value)> = Vec::new();
    let mut nearest_exact_named_poi_distance_km = None;
    if types.contains("poi") {
        let places_family = release
            .families
            .get("places")
            .filter(|family| family.entrypoints.contains_key("forward"));
        let Some(family) = places_family else {
            if params.contains_key("types") {
                return json_error("capability_unavailable", "Places data is unavailable", 503);
            }
            let features = division_candidates
                .into_iter()
                .map(|(_, feature, _)| feature)
                .collect();
            let body = data_version_body(
                &release.data_version,
                features,
                json!({"mode": "text", "places": "unavailable"}),
            );
            return versioned_response(&body, &release.data_version, 200);
        };
        let mut places = search_places(
            &loader,
            family,
            query.as_str(),
            proximity,
            true,
            autocomplete,
            limit,
        )
        .await?;
        let tokens = query_terms(query);
        let suffix_candidates = locality_suffix_candidates(&tokens, proximity.is_some(), &places);
        // Match quality is scored against the query the places lane actually
        // ran. When locality inference fires, that is the query MINUS the
        // locality suffix -- "Eiffel Tower Paris" searches places for "eiffel
        // tower", and scoring the full string against a POI named "Eiffel
        // Tower" would understate the match.
        let mut effective_query = query.to_string();
        // Bounded homonym-tolerant retry: `Rochester` is several places, and
        // the highest-ranked one need not be the one holding the POI. Try the
        // ordered localities in turn (at most LOCALITY_INFERENCE_ATTEMPT_CAP
        // routed searches) and keep the first that routes to anything. The
        // first attempt is the pre-retry behaviour, so a query that works
        // today takes exactly the same path and stops immediately.
        for (attempt_index, (place_query, inference)) in
            infer_places_locality(&loader, release.core_version(), &suffix_candidates)
                .await?
                .into_iter()
                .enumerate()
        {
            let mut routed = search_places(
                &loader,
                family,
                &place_query,
                Some((inference.longitude, inference.latitude)),
                // An inferred centroid, not a stated point: no head
                // fall-through, so an empty attempt stays the retry's signal.
                false,
                false,
                limit,
            )
            .await?;
            let (adopt, stop) = locality_attempt_disposition(attempt_index, routed.is_empty());
            if adopt {
                places_locality_inference =
                    Some(apply_places_locality_inference(&mut routed, &inference));
                places = routed;
                effective_query = place_query;
            }
            if stop {
                break;
            }
        }
        // Additive last resort. It runs only when the whole response is still
        // empty -- no division candidate, no POI from the head/phrase lanes, and
        // no locality-inferred routed result -- so it cannot displace, reorder,
        // or regress anything the ordinary lanes produced. Explicit proximity
        // no longer vetoes it: a 4-6-token proximity query whose routed lane
        // (and head fall-through) found nothing deserves the same last-resort
        // answer a no-proximity query gets, with distances attached.
        if division_candidates.is_empty() && places.is_empty() {
            let fallback =
                search_places_prefix_head_fallback(&loader, family, query.as_str(), proximity)
                    .await?;
            if !fallback.is_empty() {
                places = fallback;
                // These results answer the full query and were never routed
                // through a locality centroid, so scoring and metadata both
                // return to the query as typed.
                effective_query = query.to_string();
                places_locality_inference = None;
                places_prefix_head_fallback = prefix_head_fallback_metadata(&tokens);
            }
        }
        let normalized = poi_normalized_query(&effective_query);
        let normalized = (!normalized.is_empty()).then_some(normalized);
        nearest_exact_named_poi_distance_km = normalized.as_ref().and_then(|query| {
            places
                .iter()
                .filter(|place| poi_match_quality(&place.name, query) >= EXACT_POI_NAME_QUALITY)
                .filter_map(|place| place.distance_km)
                .min_by(|left, right| left.total_cmp(right))
        });
        poi_ranked.extend(
            places
                .iter()
                .map(|place| place_feature(place, normalized.as_ref())),
        );
    }
    // Cross-lane merge. Divisions keep their calibrated seam score except in
    // the narrow proximity-aware case `division_proximity_demotion` documents;
    // they are pushed first so score ties keep the historical division-first
    // order under the stable sort.
    let mut ranked: Vec<(f64, Value)> = Vec::new();
    for (score, mut feature, division_distance_km) in division_candidates {
        let demotion =
            division_proximity_demotion(division_distance_km, nearest_exact_named_poi_distance_km);
        let score = score * demotion;
        if demotion != 1.0 {
            feature["properties"]["relevance"] = json!(score);
        }
        ranked.push((score, feature));
    }
    ranked.extend(poi_ranked);
    ranked.sort_by(|left, right| right.0.total_cmp(&left.0));
    let mut seen = HashSet::new();
    ranked.retain(|(_, feature)| {
        feature["id"]
            .as_str()
            .is_some_and(|identity| seen.insert(identity.to_string()))
    });
    ranked.truncate(limit);
    let features = ranked.into_iter().map(|(_, feature)| feature).collect();
    let body = data_version_body(
        &release.data_version,
        features,
        text_metadata(
            &types,
            proximity,
            places_locality_inference,
            places_prefix_head_fallback,
        ),
    );
    versioned_response(&body, &release.data_version, 200)
}

pub(crate) async fn handle_reverse(
    req: Request,
    ctx: RouteContext<std::rc::Rc<Context>>,
) -> Result<Response> {
    let params: HashMap<String, String> = req
        .url()?
        .query_pairs()
        .map(|(key, value)| (key.into_owned(), value.into_owned()))
        .collect();
    let longitude = match params
        .get("lon")
        .ok_or_else(|| "lon is required".to_string())
        .and_then(|value| parse_coordinate(value, "lon", -180.0, 180.0))
    {
        Ok(value) => value,
        Err(message) => return json_error("invalid_request", &message, 400),
    };
    let latitude = match params
        .get("lat")
        .ok_or_else(|| "lat is required".to_string())
        .and_then(|value| parse_coordinate(value, "lat", -90.0, 90.0))
    {
        Ok(value) => value,
        Err(message) => return json_error("invalid_request", &message, 400),
    };
    let types = if params.contains_key("types") {
        match parse_types(params.get("types")) {
            Ok(value) => value,
            Err(message) => return json_error("invalid_request", &message, 400),
        }
    } else {
        DIVISION_TYPES
            .iter()
            .map(|value| (*value).to_string())
            .collect()
    };
    let wants_places = types.contains("poi");
    let wants_addresses = types.contains("address");
    let wants_point_reverse = wants_places || wants_addresses;
    if !wants_point_reverse && params.contains_key("radius") {
        return json_error(
            "invalid_request",
            "radius applies only when types includes poi or address",
            400,
        );
    }
    let limit = if wants_point_reverse {
        match parse_reverse_limit(params.get("limit")) {
            Ok(value) => value,
            Err(message) => return json_error("invalid_request", &message, 400),
        }
    } else {
        1
    };
    let places_radius = if wants_places {
        match parse_reverse_radius(params.get("radius"), ReverseFamily::Places) {
            Ok(value) => Some(value),
            Err(message) => return json_error("invalid_request", &message, 400),
        }
    } else {
        None
    };
    let address_radius = if wants_addresses {
        match parse_reverse_radius(params.get("radius"), ReverseFamily::Addresses) {
            Ok(value) => Some(value),
            Err(message) => return json_error("invalid_request", &message, 400),
        }
    } else {
        None
    };
    let loader = ShardLoader::with_context(&ctx.env, ctx.data.clone())?;
    let release = match load_available_release(&loader).await? {
        ReleaseAvailability::Ready(release) => release,
        ReleaseAvailability::Unavailable(response) => return Ok(response),
    };
    let division_types: HashSet<_> = types
        .iter()
        .filter(|value| DIVISION_TYPES.contains(&value.as_str()))
        .cloned()
        .collect();
    let mut features = Vec::new();
    if !division_types.is_empty() {
        let search = loader
            .reverse_geocode_version(release.core_version(), latitude, longitude)
            .await?;
        if let Some(result) = search
            .result
            .filter(|result| division_types.contains(&normalized_type(&result.subtype)))
        {
            features.push(json!({
                "type": "Feature",
                "id": result.gers_id,
                "geometry": {"type": "Point", "coordinates": [result.lon, result.lat]},
                "bbox": result.bbox,
                "properties": {
                    "name": result.primary_name,
                    "feature_type": result.subtype,
                    "distance_km": result.distance_km,
                    "confidence": result.confidence,
                    "hierarchy": result.hierarchy,
                },
            }));
        }
    }
    let mut reverse_metadata = serde_json::Map::new();
    for (family_name, family_type, radius) in [
        ("places", ReverseFamily::Places, places_radius),
        ("addresses", ReverseFamily::Addresses, address_radius),
    ] {
        let Some(radius) = radius else {
            continue;
        };
        let family = release
            .families
            .get(family_name)
            .filter(|family| family.operations.iter().any(|value| value == "reverse"));
        let Some(family) = family else {
            return json_error(
                "capability_unavailable",
                &format!("{} reverse data is unavailable", family_type.feature_type()),
                503,
            );
        };
        let Some(entrypoint) = family.entrypoints.get("reverse") else {
            return json_error(
                "capability_unavailable",
                &format!("{} reverse data is unavailable", family_type.feature_type()),
                503,
            );
        };
        let search = loader
            .reverse_construction_family(
                family.operation_version("reverse"),
                family_type,
                &entrypoint.object_key,
                entrypoint.bytes,
                &entrypoint.sha256,
                longitude,
                latitude,
                radius,
                limit,
            )
            .await;
        let search = match search {
            Ok(value) => value,
            Err(_) => {
                return json_error(
                    "capability_unavailable",
                    &format!("{} reverse data is unavailable", family_type.feature_type()),
                    503,
                )
            }
        };
        features.extend(search.hits.into_iter().map(reverse_hit_feature));
        reverse_metadata.insert(family_type.feature_type().into(), json!(search.metadata));
    }
    let body = data_version_body(
        &release.data_version,
        features,
        json!({
            "mode": "reverse",
            "query": {"longitude": longitude, "latitude": latitude},
            "reverse": reverse_metadata,
        }),
    );
    versioned_response(&body, &release.data_version, 200)
}

fn id_response_body(result: &IdLookupResult, version: &DataVersion) -> Value {
    let mut body = serde_json::to_value(result).unwrap_or_else(|_| json!({}));
    if let Some(object) = body.as_object_mut() {
        object.insert("data_version".into(), json!(version));
    }
    body
}

pub(crate) async fn handle_id(
    _req: Request,
    ctx: RouteContext<std::rc::Rc<Context>>,
) -> Result<Response> {
    let identity = ctx
        .param("id")
        .ok_or_else(|| Error::RustError("missing GERS ID".into()))?;
    if !valid_gers_id(identity) {
        return json_error(
            "invalid_request",
            "GERS ID must be a 32-digit or canonical hyphenated UUID",
            400,
        );
    }
    let loader = ShardLoader::with_context(&ctx.env, ctx.data.clone())?;
    let release = match load_available_release(&loader).await? {
        ReleaseAvailability::Ready(release) => release,
        ReleaseAvailability::Unavailable(response) => return Ok(response),
    };
    let lookup = loader
        .lookup_id_version(release.core_version(), identity)
        .await;
    let lookup = match lookup {
        Ok(value) => value,
        Err(error) if format!("{error:?}").contains(NOT_FOUND_SENTINEL) => {
            return json_error("capability_unavailable", "ID index is unavailable", 503)
        }
        Err(error) => return Err(error),
    };
    let Some(result) = lookup.result else {
        return json_error("not_found", "GERS ID was not found", 404);
    };
    let body = id_response_body(&result, &release.data_version);
    versioned_json_response(&body, &release.data_version, 200)
}

/// Local before/after evidence for the proximity-lane wave over a real
/// promoted slice (the Monaco harness output of
/// `scripts/promote_construction_slice.py`), mirroring
/// `search_places_construction`'s routed/head flow with filesystem reads in
/// place of the `ShardLoader`. `#[ignore]`d like the other local-slice
/// harnesses; drive it with:
///
/// `PLACES_PROMOTED_FAMILY_DIR` -- the promoted `.../families/places` dir.
/// `PROXIMITY_EXPERIMENT_QUERY` -- the query text.
/// `PROXIMITY_EXPERIMENT_POINT` -- `longitude,latitude` bias point.
#[cfg(test)]
mod proximity_experiment {
    use super::{place_score, poi_normalized_query, NormalizedQuery};
    use crate::places_construction_v1::{
        compose_entity_phrase_candidates, construction_cell, entity_phrase_key,
        entity_phrase_token_groups, head_shard_id, head_shard_lookup, merge_head_candidates,
        merge_routed_candidates, neighbor_construction_cells, record_projection, routed_fetch_plan,
        routed_lookup, HeadRoutingManifest, PlacesRouting, PlacesV1Record, HEAD_QUERY_TOKEN_CAP,
    };
    use crate::places_pages::{query_terms, PlaceProjection};
    use std::path::Path;

    fn local_routed_records(
        root: &Path,
        routing: &PlacesRouting,
        cell: &str,
        tokens: &[String],
    ) -> Vec<PlacesV1Record> {
        let Some(plan) = routed_fetch_plan(routing, cell, tokens).expect("tiling holds") else {
            return Vec::new();
        };
        let mut per_token: Vec<Option<Vec<PlacesV1Record>>> =
            (0..tokens.len()).map(|_| None).collect();
        for (object, token_indexes) in plan {
            let bytes = std::fs::read(root.join("objects").join(object)).expect("routed object");
            for index in token_indexes {
                let records = routed_lookup(&bytes, cell, &tokens[index]).expect("routed decode");
                if records.is_empty() {
                    return Vec::new();
                }
                per_token[index] = Some(records);
            }
        }
        let per_token: Vec<_> = per_token.into_iter().map(Option::unwrap).collect();
        merge_routed_candidates(tokens, per_token).expect("routed merge")
    }

    fn local_head_records(
        root: &Path,
        head: &HeadRoutingManifest,
        tokens: &[String],
    ) -> Vec<PlacesV1Record> {
        if tokens.is_empty() || tokens.len() > HEAD_QUERY_TOKEN_CAP {
            return Vec::new();
        }
        let mut phrase_groups = Vec::new();
        if head.admits_entity_phrases() {
            for phrase_tokens in entity_phrase_token_groups(tokens) {
                let mut phrase_records = Vec::new();
                if let Some(phrase_key) = entity_phrase_key(phrase_tokens) {
                    let shard_id = head_shard_id(&phrase_key, head.shard_bits);
                    if let Some(shard) = head.shard(shard_id) {
                        let bytes = std::fs::read(root.join("objects").join(&shard.path))
                            .expect("head shard");
                        let records =
                            head_shard_lookup(&bytes, shard_id, head.shard_bits, &phrase_key)
                                .expect("head decode");
                        phrase_records =
                            super::validate_entity_phrase_records(phrase_tokens, records);
                    }
                }
                phrase_groups.push(phrase_records);
            }
        }
        let mut per_token = Vec::with_capacity(tokens.len());
        let mut ordinary_complete = true;
        for token in tokens {
            let shard_id = head_shard_id(token, head.shard_bits);
            let Some(shard) = head.shard(shard_id) else {
                ordinary_complete = false;
                break;
            };
            let bytes = std::fs::read(root.join("objects").join(&shard.path)).expect("head shard");
            let records =
                head_shard_lookup(&bytes, shard_id, head.shard_bits, token).expect("head decode");
            if records.is_empty() {
                ordinary_complete = false;
                break;
            }
            per_token.push(records);
        }
        let ordinary = if ordinary_complete {
            merge_head_candidates(tokens, per_token).expect("head merge")
        } else {
            Vec::new()
        };
        compose_entity_phrase_candidates(phrase_groups, ordinary).expect("phrase composition")
    }

    fn projections(
        records: &[PlacesV1Record],
        longitude: f64,
        latitude: f64,
    ) -> Vec<PlaceProjection> {
        let mut results: Vec<PlaceProjection> = records.iter().map(record_projection).collect();
        for place in &mut results {
            place.distance_km = Some(super::haversine_km(
                latitude,
                longitude,
                f64::from(place.latitude),
                f64::from(place.longitude),
            ));
        }
        results
    }

    fn print_ranking(label: &str, places: &[PlaceProjection], query: &NormalizedQuery) {
        let mut scored: Vec<(f64, &PlaceProjection)> = places
            .iter()
            .map(|place| (place_score(place, Some(query)), place))
            .collect();
        scored.sort_by(|left, right| right.0.total_cmp(&left.0));
        println!("== {label}: {} candidates", scored.len());
        for (rank, (score, place)) in scored.iter().take(10).enumerate() {
            println!(
                "  {:>2}. score {:.4}  {:>7.2} km  {}",
                rank + 1,
                score,
                place.distance_km.unwrap_or(f64::NAN),
                place.name
            );
        }
    }

    #[test]
    #[ignore = "requires a locally promoted slice; driven manually"]
    fn local_promoted_slice_proximity_experiment() {
        let root = std::path::PathBuf::from(
            std::env::var("PLACES_PROMOTED_FAMILY_DIR")
                .expect("PLACES_PROMOTED_FAMILY_DIR is required"),
        );
        let query_text = std::env::var("PROXIMITY_EXPERIMENT_QUERY").expect("query is required");
        let point = std::env::var("PROXIMITY_EXPERIMENT_POINT").expect("point is required");
        let (longitude, latitude) = point
            .split_once(',')
            .map(|(lon, lat)| {
                (
                    lon.trim().parse::<f64>().expect("longitude"),
                    lat.trim().parse::<f64>().expect("latitude"),
                )
            })
            .expect("point is longitude,latitude");
        let routing_text =
            std::fs::read_to_string(root.join("routing.json")).expect("read routing.json");
        let routing = PlacesRouting::parse(&routing_text).expect("routing parses");
        let head_text =
            std::fs::read_to_string(root.join("objects").join(&routing.head.manifest_object))
                .expect("read head routing manifest");
        let head = HeadRoutingManifest::parse(&head_text).expect("head manifest parses");
        assert!(head.agrees_with(&routing.head));
        let tokens = query_terms(&query_text);
        // Keep this constructor aligned with the production places scoring
        // seam so non-ASCII local evidence uses the retrieval-compatible fold.
        let normalized = poi_normalized_query(&query_text);
        println!(
            "query {query_text:?} at ({longitude}, {latitude}), tokens {tokens:?}, cell {:?}",
            construction_cell(longitude, latitude)
        );

        // BEFORE: one construction cell, no fall-through, no distance term.
        // The pre-wave score is exactly `place_score` with distance_km None;
        // the true distance is shown alongside so the arbitrary ordering is
        // visible for what it is.
        let before_records = construction_cell(longitude, latitude)
            .map(|cell| local_routed_records(&root, &routing, &cell, &tokens))
            .unwrap_or_default();
        let before = projections(&before_records, longitude, latitude);
        let mut before_scored: Vec<(f64, &PlaceProjection)> = before
            .iter()
            .map(|place| {
                let mut pre_wave = place.clone();
                pre_wave.distance_km = None;
                (place_score(&pre_wave, Some(&normalized)), place)
            })
            .collect();
        before_scored.sort_by(|left, right| right.0.total_cmp(&left.0));
        println!(
            "== BEFORE (single cell, no distance term): {} candidates",
            before_scored.len()
        );
        for (rank, (score, place)) in before_scored.iter().take(10).enumerate() {
            println!(
                "  {:>2}. score {:.4}  {:>7.2} km  {}",
                rank + 1,
                score,
                place.distance_km.unwrap_or(f64::NAN),
                place.name
            );
        }

        // AFTER: primary cell + neighbor probe + head fall-through, distance
        // term active.
        let mut after_records = Vec::new();
        let mut lane = "routed";
        if let Some(cell) = construction_cell(longitude, latitude) {
            after_records = local_routed_records(&root, &routing, &cell, &tokens);
            let expand_empty_primary =
                after_records.is_empty() && tokens.len() > super::HEAD_QUERY_TOKEN_CAP;
            let neighbors = neighbor_construction_cells(longitude, latitude, expand_empty_primary);
            println!("neighbor cells probed: {neighbors:?}");
            for neighbor in &neighbors {
                after_records.extend(local_routed_records(&root, &routing, neighbor, &tokens));
            }
            let mut seen = std::collections::HashSet::new();
            after_records.retain(|record| {
                seen.insert((
                    record.id.clone(),
                    record.source_object_index,
                    record.source_row_group,
                    record.source_row_index,
                ))
            });
        }
        if super::routed_lane_falls_through_to_head(true, after_records.is_empty(), tokens.len()) {
            lane = "head fall-through";
            after_records = local_head_records(&root, &head, &tokens);
        }
        let after = projections(&after_records, longitude, latitude);
        println!("lane: {lane}");
        print_ranking(
            "AFTER (neighbor probe + fall-through + distance term)",
            &after,
            &normalized,
        );
    }
}

#[cfg(test)]
mod seam_tests {
    use super::{
        division_proximity_demotion, effective_place_prominence, place_score, poi_match_quality,
        poi_normalized_query, routed_lane_falls_through_to_head, NormalizedQuery,
        DIVISION_PROXIMITY_DEMOTION, POI_PRIOR_CAP, PROXIMITY_CONFIDENCE_SHRINK,
        PROXIMITY_DISTANCE_BAND,
    };
    use crate::places_pages::PlaceProjection;

    fn place(name: &str, confidence: f32) -> PlaceProjection {
        PlaceProjection {
            id: "id".into(),
            latitude: 47.6,
            longitude: -122.3,
            confidence,
            prominence: 0.0,
            name: name.into(),
            category: "bank".into(),
            locality: "Seattle".into(),
            region: "WA".into(),
            country: "US".into(),
            distance_km: None,
        }
    }

    /// The live `q=Seattle` failure: ten POIs at a saturated confidence of 1.0
    /// buried the city entirely. The city scored 0.8408; every POI scored 1.0
    /// purely because Overture marks it as existing.
    const SEATTLE_DIVISION_SCORE: f64 = 0.8408;

    #[test]
    fn a_saturated_non_matching_poi_no_longer_outranks_a_division() {
        let query = NormalizedQuery::new("Seattle");
        let ups_store = place("The UPS Store", 1.0);
        let score = place_score(&ups_store, Some(&query));
        assert!(
            score < SEATTLE_DIVISION_SCORE,
            "a confidence-1.0 POI that does not match the query text scored {score}, \
             which must be below the division's {SEATTLE_DIVISION_SCORE}"
        );
        // It matches nothing, so only the capped prior contributes.
        assert!((score - 0.5 * POI_PRIOR_CAP / 2.0).abs() < 1e-9);
    }

    #[test]
    fn an_exactly_named_poi_still_loses_to_a_prominent_division_but_leads_the_pack() {
        let query = NormalizedQuery::new("Seattle");
        let exact = place_score(&place("Seattle", 1.0), Some(&query));
        let unrelated = place_score(&place("The UPS Store", 1.0), Some(&query));
        assert!(
            exact > unrelated,
            "an exact name match must lead other POIs"
        );
        assert!(
            exact < SEATTLE_DIVISION_SCORE,
            "a POI named Seattle should not outrank the city of Seattle"
        );
    }

    #[test]
    fn a_landmark_poi_outranks_a_weak_division_score() {
        // Nothing is called "Eiffel Tower" among divisions, so the POI must
        // win outright rather than being suppressed by the calibration.
        let query = NormalizedQuery::new("Eiffel Tower");
        let tower = place_score(&place("Eiffel Tower", 0.8), Some(&query));
        let hotel = place_score(&place("Hotel Eiffel Blomet", 1.0), Some(&query));
        assert!(
            tower > hotel,
            "exact beats a longer name containing the query"
        );
        assert!(
            tower >= 0.5,
            "an exact landmark match stays strongly ranked"
        );
    }

    #[test]
    fn without_a_text_query_the_previous_confidence_score_stands() {
        // Reverse and proximity-only lookups have nothing to match against.
        let unchanged = place_score(&place("The UPS Store", 0.75), None);
        assert!((unchanged - 0.75).abs() < 1e-9);
    }

    #[test]
    fn the_poi_prior_cannot_outvote_a_match_quality_step() {
        // The whole point of the cap: the best possible prior contribution must
        // be smaller than the gap between adjacent match-quality rungs, so
        // confidence can order ties but never overturn a better text match.
        let query = NormalizedQuery::new("Seattle");
        let weak_match_high_confidence = place_score(&place("Seattle Bank", 1.0), Some(&query));
        let strong_match_zero_confidence = place_score(&place("Seattle", 0.0), Some(&query));
        assert!(
            strong_match_zero_confidence > weak_match_high_confidence,
            "an exact match at confidence 0 must beat a prefix match at confidence 1"
        );
    }

    #[test]
    fn full_name_exactness_breaks_comma_truncation_ties() {
        let query = NormalizedQuery::new("Taj Mahal");
        assert_eq!(poi_match_quality("Taj Mahal", &query), 1.0);
        assert_eq!(poi_match_quality("Taj Mahal, Agra, India", &query), 0.97);
    }

    #[test]
    fn multi_token_partial_quality_rewards_compact_token_coverage() {
        let query = NormalizedQuery::new("SickKids Toronto");
        let exact_entity = poi_match_quality("SickKids", &query);
        let foundation = poi_match_quality("SickKids Foundation", &query);

        assert!(exact_entity > foundation);
        assert!(exact_entity < 0.8);
    }

    /// Retrieval tokenization is NFKD (build tokenizer
    /// `nfkd-lower-stripmark-cjk-bigram-v4`, mirrored by
    /// `places_pages::normalized_words`), so `skoda` retrieves "Škoda Muzeum".
    /// Rank-time match quality must agree, or the retrieved record scores 0
    /// and is floored. Czech/Polish/Turkish exact hits were structurally
    /// unable to rank before the scoring fold matched retrieval.
    #[test]
    fn nfkd_scoring_parity_ascii_query_scores_diacritic_name_exact() {
        let cases = [
            // Czech: š decomposes under NFKD.
            ("Škoda Muzeum", "skoda muzeum"),
            // Polish: ó/ź decompose; ł is stroked (non-decomposable) and is
            // folded by the explicit one-to-one scoring fold.
            ("Łódź Kaliska", "lodz kaliska"),
            // Turkish İ: NFKD → I + combining dot above → lowercase i.
            ("İstanbul Modern", "istanbul modern"),
            // Turkish dotless ı: non-decomposable, explicit one-to-one fold.
            ("Kızılay Meydanı", "kizilay meydani"),
            // Romanian ţ and Hungarian ő decompose under NFKD.
            ("Piaţa Unirii", "piata unirii"),
            ("Hősök tere", "hosok tere"),
        ];
        for (name, query) in cases {
            let quality = poi_match_quality(name, &poi_normalized_query(query));
            assert_eq!(
                quality, 1.0,
                "{query:?} is an exact NFKD hit on {name:?} and must score 1.0, got {quality}"
            );
        }
    }

    /// Folding the query is as important as folding the candidate: users may
    /// type the native spelling as well as its ASCII approximation. If
    /// `poi_normalized_query` regresses to a bare `NormalizedQuery`, the name
    /// side remains folded and these exact self-matches fall to zero.
    #[test]
    fn nfkd_scoring_parity_preserves_native_query_spellings() {
        for name in [
            "Škoda Muzeum",
            "Łódź Kaliska",
            "Kızılay Meydanı",
            "Hősök tere",
            "Piaţa Unirii",
        ] {
            let quality = poi_match_quality(name, &poi_normalized_query(name));
            assert_eq!(quality, 1.0, "native query {name:?} scored {quality}");
        }
    }

    /// NFKD maps several compatibility comma forms to ASCII `,`, but ASCII
    /// comma is syntax to the scoring ladder: it truncates both a
    /// `NormalizedQuery` and a candidate name. The scoring fold guards those
    /// compatibility forms so it cannot invent a truncation point that the
    /// raw text did not contain.
    #[test]
    fn compatibility_commas_do_not_truncate_places_scoring() {
        for comma in ['\u{fe10}', '\u{fe50}', '\u{ff0c}'] {
            let name = format!("東京{comma}渋谷店");
            let query = poi_normalized_query(&name);
            assert!(!query.is_empty(), "compatibility comma emptied {name:?}");
            assert_eq!(poi_match_quality(&name, &query), 1.0, "{name:?}");

            let leading = poi_normalized_query(&format!("{comma}東京"));
            assert!(
                !leading.is_empty(),
                "leading compatibility comma collapsed the places lane"
            );
        }
    }

    /// The build/query tokenizer folds Greek final sigma to ordinary sigma.
    /// Scoring must do the same on both sides of the comparison.
    #[test]
    fn scoring_fold_preserves_final_sigma_parity() {
        let quality = poi_match_quality("ΟΣ", &poi_normalized_query("ος"));
        assert_eq!(quality, 1.0);
    }

    /// Lowercasing and combining-mark removal are explicit stages of the
    /// tokenizer contract. U+0130 exercises both and must end as one `i`, not
    /// an `i` followed by a combining dot.
    #[test]
    fn scoring_fold_strips_marks_from_lowercase_output() {
        assert_eq!(super::fold_for_scoring("İ"), "i");
    }

    /// Real `names.primary` values from the production corpus (2026-06-17.0,
    /// pulled 2026-08-06), one per newly-covered character class, paired with
    /// the ASCII spelling a user actually types. Every pair is a full-name
    /// query, so parity means exactly 1.0 — before the fix each scored 0 and
    /// was floored by the `quality > 0.0` gate. 2,612,396 corpus records
    /// (3.45%) contain at least one character in this class.
    #[test]
    fn corpus_names_with_nfkd_only_characters_score_for_ascii_queries() {
        let cases = [
            ("Marković Winery and Estate", "markovic winery and estate"), // ć
            ("Kovačić Renting", "kovacic renting"),                       // č
            ("Kaple Tří králů", "kaple tri kralu"),                       // ř, í, ů
            ("Małgorzata Sikora", "malgorzata sikora"),                   // ł
            ("Radio Na Góralską Nutę", "radio na goralska nute"),         // ą, ę
            ("Gia đình Nazareth", "gia dinh nazareth"),                   // đ, ì
            ("Turunç Pınarı Koyu", "turunc pinari koyu"),                 // ı
            ("İsmailin Yeri", "ismailin yeri"),                           // İ
            ("Parcul Naţional Zion", "parcul national zion"),             // ţ
            ("Orașul Artelor și Științei", "orasul artelor si stiintei"), // ș, ț
            ("Gerincjóga Győr", "gerincjoga gyor"),                       // ő
            ("Sanela Kanjiža Real Estate", "sanela kanjiza real estate"), // ž
            ("Nutricionista Juraj Botoš", "nutricionista juraj botos"),   // š
            ("Uterqűe Main Office", "uterque main office"),               // ű
            ("Dubaj Letiště", "dubaj letiste"),                           // ě
        ];
        for (name, query) in cases {
            let quality = poi_match_quality(name, &poi_normalized_query(query));
            assert!(quality > 0.0, "{name:?} floored for {query:?}");
            assert_eq!(
                quality, 1.0,
                "{query:?} is the full ASCII spelling of {name:?}, got {quality}"
            );
        }
        // The lower ladder rungs work through the fold too: a single-word
        // ASCII query is a word-boundary prefix of the folded name.
        let quality = poi_match_quality("Małgorzata Sikora", &poi_normalized_query("malgorzata"));
        assert_eq!(quality, 0.9);
    }

    /// Control: the small Western-European fold table already handled é-class
    /// names. That behavior must hold before AND after the NFKD parity fix.
    #[test]
    fn western_european_diacritics_already_scored_and_still_do() {
        for (name, query) in [
            ("Café de Flore", "cafe de flore"),
            ("Zürich Hauptbahnhof", "zurich hauptbahnhof"),
            ("São Paulo Fan Fest", "sao paulo fan fest"),
        ] {
            let quality = poi_match_quality(name, &poi_normalized_query(query));
            assert_eq!(quality, 1.0, "{name:?} vs {query:?} scored {quality}");
        }
    }

    /// The query-side scoring fold must be identity on ASCII: with the same
    /// name-side fold used by both calls, every rung produces the same value
    /// through `poi_normalized_query` as through a bare `NormalizedQuery`.
    /// This pins the query seam; the non-ASCII tests above pin the name seam.
    #[test]
    fn nfkd_scoring_fold_is_identity_on_ascii() {
        let names = [
            "Seattle",
            "Seattle Bank",
            "The UPS Store",
            "Taj Mahal",
            "Taj Mahal, Agra, India",
            "SickKids",
            "SickKids Foundation",
            "Eiffel Tower",
            "Hotel Eiffel Blomet",
            "Space Needle",
            "McDonald's",
            "H&M",
            "Big Ben",
            "Paris Township",
            "Parisville",
            "Paradise",
            "1st Avenue Deli",
        ];
        let queries = [
            "Seattle",
            "seattle bank",
            "Taj Mahal",
            "SickKids Toronto",
            "Eiffel Tower",
            "space needle",
            "mcdonalds",
            "h and m",
            "big ben london",
            "paris",
            "1st avenue",
        ];
        for name in names {
            for query in queries {
                let through_fold = poi_match_quality(name, &poi_normalized_query(query));
                let bare = poi_match_quality(name, &NormalizedQuery::new(query));
                assert_eq!(
                    through_fold, bare,
                    "ASCII identity violated for name {name:?} query {query:?}"
                );
            }
        }
    }

    fn place_at(name: &str, confidence: f32, distance_km: f64) -> PlaceProjection {
        let mut place = place(name, confidence);
        place.distance_km = Some(distance_km);
        place
    }

    fn prominent_place_at(
        name: &str,
        confidence: f32,
        prominence: f32,
        distance_km: f64,
    ) -> PlaceProjection {
        let mut place = place_at(name, confidence, distance_km);
        place.prominence = prominence;
        place
    }

    /// The live Times Square failure: ten same-name, same-prior Starbucks tied
    /// on score and ordered arbitrarily at 67.0, 38.5, 52.3, 24.1 km. With a
    /// distance term, nearer wins deterministically.
    #[test]
    fn nearer_wins_decisively_among_equal_text_matches() {
        let query = NormalizedQuery::new("starbucks");
        let near = place_score(&place_at("Starbucks", 0.77, 0.5), Some(&query));
        let far = place_score(&place_at("Starbucks", 0.77, 24.1), Some(&query));
        assert!(
            near > far,
            "equal text matches must order by distance: near {near} vs far {far}"
        );
    }

    /// Confidence is an existence byte; under explicit proximity it must not
    /// outvote distance among same-name records. The shrink guarantees the
    /// distance band (0.0375) dominates the confidence band (0.01).
    #[test]
    fn distance_outvotes_confidence_noise_among_same_names() {
        let query = NormalizedQuery::new("starbucks");
        let near_low_confidence = place_score(&place_at("Starbucks", 0.0, 0.5), Some(&query));
        let far_high_confidence = place_score(&place_at("Starbucks", 1.0, 24.1), Some(&query));
        assert!(
            near_low_confidence > far_high_confidence,
            "a nearby record must beat a distant one regardless of confidence: \
             {near_low_confidence} vs {far_high_confidence}"
        );
        let sub_rung_budget =
            PROXIMITY_DISTANCE_BAND + 0.5 * PROXIMITY_CONFIDENCE_SHRINK * POI_PRIOR_CAP;
        assert!(
            sub_rung_budget > 0.03 && sub_rung_budget < 0.07,
            "the sub-rung budget ({sub_rung_budget}) must cross only the 0.03 comma sub-rung"
        );
    }

    /// The POI_PRIOR_CAP discipline extends to distance: the whole non-text
    /// band stays under the binding 0.07 gap between a comma-qualified full
    /// name and a whole-word match.
    #[test]
    fn a_distance_bonus_cannot_overturn_a_match_quality_step() {
        let query = NormalizedQuery::new("Taj Mahal");
        let qualified_far = place_score(&place_at("Taj Mahal, Agra", 0.0, 1000.0), Some(&query));
        let whole_word_at_zero = place_score(&place_at("Taj Mahal Hotel", 1.0, 0.0), Some(&query));
        assert_eq!(poi_match_quality("Taj Mahal, Agra", &query), 0.97);
        assert_eq!(poi_match_quality("Taj Mahal Hotel", &query), 0.9);
        assert!(
            qualified_far > whole_word_at_zero,
            "a qualified full-name match must survive the maximum bonus a whole-word match earns: \
             {qualified_far} vs {whole_word_at_zero}"
        );
    }

    /// The distance budget intentionally crosses the 0.03 exact/comma
    /// sub-rung: explicit proximity may prefer a nearby qualified rendering of
    /// the same name over an exact rendering hundreds of kilometres away.
    #[test]
    fn proximity_can_cross_the_exact_comma_sub_rung() {
        let query = NormalizedQuery::new("Taj Mahal");
        let exact_far = place_score(&place_at("Taj Mahal", 0.0, 500.0), Some(&query));
        let qualified_near = place_score(&place_at("Taj Mahal, Agra", 0.0, 0.0), Some(&query));
        assert!(qualified_near > exact_far);
    }

    /// Calibrated prominence deliberately keeps its full band. This residual
    /// is pre-existing and documented: at the current constants a same-name
    /// sibling with prominence 0.08 can narrowly beat a much nearer sibling
    /// with no prominence. The proximity wave narrows, but does not eliminate,
    /// that fame-versus-distance tradeoff.
    #[test]
    fn calibrated_prominence_can_still_outvote_distance_among_siblings() {
        let query = NormalizedQuery::new("starbucks");
        let near = place_score(&place_at("Starbucks", 0.77, 0.5), Some(&query));
        let far_prominent = place_score(
            &prominent_place_at("Starbucks", 0.77, 0.08, 67.0),
            Some(&query),
        );
        assert!(far_prominent > near);
    }

    /// Without a distance (every no-proximity query), the score is
    /// byte-identical to the pre-wave formula -- the calibrated seam and all
    /// pinned bands above rest on this.
    #[test]
    fn scores_without_distance_are_the_pre_wave_scores() {
        let query = NormalizedQuery::new("Seattle");
        let exact = place_score(&place("Seattle", 1.0), Some(&query));
        assert!((exact - (1.0 + 0.5 * POI_PRIOR_CAP) / 2.0).abs() < 1e-9);
        let unrelated = place_score(&place("The UPS Store", 1.0), Some(&query));
        assert!((unrelated - 0.5 * POI_PRIOR_CAP / 2.0).abs() < 1e-9);
    }

    /// Explicit proximity is a bias, never a veto: an empty routed answer for
    /// a head-servable query falls through to the head; wider queries are left
    /// to the additive prefix-head last resort; a non-empty routed answer is
    /// authoritative.
    #[test]
    fn empty_routed_lane_falls_through_only_for_head_servable_queries() {
        assert!(routed_lane_falls_through_to_head(true, true, 1));
        assert!(routed_lane_falls_through_to_head(true, true, 3));
        assert!(!routed_lane_falls_through_to_head(true, true, 4));
        assert!(!routed_lane_falls_through_to_head(true, false, 2));
    }

    /// An INFERRED locality centroid never falls through. `handle_forward`'s
    /// bounded homonym retry reads an empty routed attempt as "try the next
    /// locality"; a global-head answer there would end the retry at attempt 0
    /// and be reported as `routing: locality_centroid` for the wrong division.
    #[test]
    fn an_inferred_locality_centroid_never_falls_through_to_the_head() {
        for token_count in 1..=3 {
            assert!(!routed_lane_falls_through_to_head(false, true, token_count));
            assert!(routed_lane_falls_through_to_head(true, true, token_count));
        }
    }

    /// The demotion is deliberately narrow: it needs explicit proximity, a
    /// division beyond distance relevance, AND an exact-name POI to weigh it
    /// against. Everything else -- the whole calibrated no-proximity seam --
    /// keeps its score to the last bit.
    #[test]
    fn the_division_demotion_fires_only_with_all_three_conditions() {
        assert_eq!(division_proximity_demotion(None, Some(20.0)), 1.0);
        assert_eq!(division_proximity_demotion(Some(20.0), Some(20.0)), 1.0);
        assert_eq!(division_proximity_demotion(Some(1700.0), None), 1.0);
        assert_eq!(division_proximity_demotion(Some(1700.0), Some(1200.0)), 1.0);
        assert_eq!(
            division_proximity_demotion(Some(1700.0), Some(20.0)),
            DIVISION_PROXIMITY_DEMOTION
        );
    }

    /// A global-head fall-through can surface an exact-name POI anywhere on
    /// Earth. That distant POI must not trigger demotion of a division that is
    /// itself nearer to the stated bias point (the Springfield-shaped
    /// interaction found in review).
    #[test]
    fn a_far_head_poi_does_not_demote_a_nearer_division() {
        assert_eq!(division_proximity_demotion(Some(200.0), Some(1300.0)), 1.0);
    }

    /// The division demotion is intentionally a hard relevance-radius rule.
    /// Pin its current division-vs-division consequence so a future smoothing
    /// change is measured rather than accidental.
    #[test]
    fn division_demotion_radius_can_reorder_nearby_division_scores() {
        let nearby_exact_poi = Some(1.0);
        let inside = 0.57 * division_proximity_demotion(Some(99.0), nearby_exact_poi);
        let outside = 0.60 * division_proximity_demotion(Some(101.0), nearby_exact_poi);
        assert!(inside > outside);
    }

    /// The live McDonald defect, in the live numbers: McDonald County, MO
    /// (relevance 0.5749, 1,700 km from Times Square) outranked every actual
    /// McDonald's (0.5199). Demoted, the county drops below the POI; a major
    /// city inside the relevance radius is untouched and still wins.
    #[test]
    fn a_distant_homonym_division_no_longer_beats_a_nearby_exact_poi() {
        const COUNTY_SCORE: f64 = 0.5749;
        const POI_SCORE: f64 = 0.5199;
        let (county, poi) = (COUNTY_SCORE, POI_SCORE);
        assert!(county > poi, "the pre-wave defect");
        let demoted = COUNTY_SCORE * division_proximity_demotion(Some(1700.0), Some(24.1));
        assert!(
            demoted < POI_SCORE,
            "the demoted county ({demoted}) must rank below the exact-name POI ({POI_SCORE})"
        );
        // A big city queried by its own name with proximity inside it keeps
        // its score entirely...
        let city = SEATTLE_DIVISION_SCORE * division_proximity_demotion(Some(15.0), Some(0.5));
        assert_eq!(city, SEATTLE_DIVISION_SCORE);
        // ...and even a big city OUTSIDE the radius still beats the strongest
        // non-prominent exact-name POI after demotion, so `q=chicago` with
        // NYC proximity cannot surface a pizza joint above the city.
        let distant_city =
            SEATTLE_DIVISION_SCORE * division_proximity_demotion(Some(1200.0), Some(0.5));
        let query = NormalizedQuery::new("Seattle");
        let strongest_nearby_poi = place_score(&place_at("Seattle", 1.0, 0.0), Some(&query));
        assert!(distant_city > strongest_nearby_poi);
    }

    #[test]
    fn primary_category_corrects_alternate_inflation() {
        let mut false_big_ben = place("Big Ben", 0.84);
        false_big_ben.category = "fountain".into();
        false_big_ben.prominence = 0.502;
        let mut real_big_ben = place("Big Ben", 0.94);
        real_big_ben.category = "landmark_and_historical_building".into();
        real_big_ben.prominence = 0.349;
        let mut terminal = place("Terminal 1", 0.9);
        terminal.category = "airport_terminal".into();
        terminal.prominence = 0.902;

        assert!((effective_place_prominence(&false_big_ben) - 0.08).abs() < 1e-9);
        assert!((effective_place_prominence(&real_big_ben) - 0.85).abs() < 1e-9);
        assert!((effective_place_prominence(&terminal) - 0.90).abs() < 1e-9);
        let query = NormalizedQuery::new("Big Ben");
        assert!(
            place_score(&real_big_ben, Some(&query)) > place_score(&false_big_ben, Some(&query))
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sha() -> String {
        "a".repeat(64)
    }

    fn catalog() -> V2Catalog {
        V2Catalog {
            schema: CATALOG_SCHEMA.into(),
            latest: "2026-07-19.1".into(),
            releases: vec![CatalogEntry {
                geocoder_build: "2026-07-19.1".into(),
                overture_release: "2026-06-17.0".into(),
                manifest_key: "v2/releases/2026-07-19.1/release.json".into(),
                manifest_sha256: sha(),
                release_digest: sha(),
            }],
            catalog_digest: sha(),
        }
    }

    fn release_value() -> Value {
        json!({
            "schema": RELEASE_SCHEMA,
            "generated_at": "2026-07-19T00:00:00+00:00",
            "geocoder_build": "2026-07-19.1",
            "overture_release": "2026-06-17.0",
            "data_version": {
                "overture_release": "2026-06-17.0",
                "geocoder_build": "2026-07-19.1"
            },
            "legacy_core": {
                "version": "2026-07-18.0",
                "manifest_key": "2026-07-18.0/release-manifest.json",
                "manifest_sha256": sha(),
                "entrypoints": {
                    "feature_lookup": "2026-07-18.0/id-collection.json",
                    "forward": "2026-07-18.0/collection.json",
                    "reverse": "2026-07-18.0/reverse-collection.json"
                }
            },
            "families": {
                "places": {
                    "source": {
                        "kind": "family_slice",
                        "version": "slice-2026-07-19.0",
                        "manifest_key": "slice-2026-07-19.0/slice-manifest.json",
                        "manifest_sha256": sha()
                    },
                    "manifest_key": "slice-2026-07-19.0/families/places/family-manifest.json",
                    "manifest_digest": sha(),
                    "manifest_sha256": sha(),
                    "versions": {
                        "format": PLACES_FORMAT_VERSION,
                        "tokenizer": TOKENIZER_VERSION,
                        "normalization": null
                    },
                    "coverage": {
                        "name": "world",
                        "bbox": [-180.0, -90.0, 180.0, 90.0],
                        "bbox_scope": "exact"
                    },
                    "operations": ["forward"],
                    "entrypoints": {"forward": {
                        "object_key": "slice-2026-07-19.0/families/places/catalog.pcat",
                        "bytes": 123,
                        "sha256": sha()
                    }}
                },
                "addresses": {
                    "source": {
                        "kind": "family_slice",
                        "version": "slice-2026-07-19.0",
                        "manifest_key": "slice-2026-07-19.0/slice-manifest.json",
                        "manifest_sha256": sha()
                    },
                    "manifest_key": "slice-2026-07-19.0/families/addresses/family-manifest.json",
                    "manifest_digest": sha(),
                    "manifest_sha256": sha(),
                    "versions": {
                        "format": ADDRESS_FORMAT_VERSION,
                        "tokenizer": null,
                        "normalization": ADDRESS_NORMALIZATION_VERSION
                    },
                    "coverage": {
                        "name": "world",
                        "bbox": [-180.0, -90.0, 180.0, 90.0],
                        "bbox_scope": "exact"
                    },
                    "operations": ["structured_forward"],
                    "entrypoints": {"structured_forward": {
                        "object_key": "slice-2026-07-19.0/families/addresses/address-collection.json",
                        "bytes": 123,
                        "sha256": sha()
                    }}
                }
            },
            "operations": {
                "feature_lookup": ["id"],
                "forward": ["divisions", "places"],
                "reverse": ["divisions"],
                "structured_forward": ["addresses"]
            },
            "release_digest": sha()
        })
    }

    fn release() -> V2Release {
        serde_json::from_value(release_value()).unwrap()
    }

    fn sign_control_document(mut value: Value, digest_field: &str) -> String {
        value.as_object_mut().unwrap().remove(digest_field);
        let digest = canonical_control_digest(&value).unwrap();
        value
            .as_object_mut()
            .unwrap()
            .insert(digest_field.to_string(), Value::String(digest));
        serde_json::to_string(&value).unwrap()
    }

    #[test]
    fn control_digest_matches_python_sorted_compact_ascii_json() {
        let value = json!({
            "z": {"items": 3, "enabled": true},
            "message": "quote=\" slash=/ backslash=\\",
            "a": [-180.0, -90.0, 180.0, 90.0]
        });
        assert_eq!(
            canonical_control_digest(&value).unwrap(),
            "3a5f3ccbf3cf28c644ab6831fed34365ef23faadb54dcf74529a39a30b03075b"
        );
    }

    #[test]
    fn raw_control_digest_gate_accepts_current_release_and_catalog_shapes() {
        let release_text = sign_control_document(release_value(), "release_digest");
        let parsed_release: V2Release =
            parse_verified_control_document(&release_text, "release_digest", "release fixture")
                .unwrap();

        let release_sha = format!("{:x}", Sha256::digest(release_text.as_bytes()));
        let catalog_value = json!({
            "schema": CATALOG_SCHEMA,
            "generated_at": "2026-07-19T00:00:00+00:00",
            "latest": "2026-07-19.1",
            "releases": [{
                "geocoder_build": "2026-07-19.1",
                "overture_release": "2026-06-17.0",
                "manifest_key": "v2/releases/2026-07-19.1/release.json",
                "manifest_sha256": release_sha,
                "release_digest": parsed_release.release_digest,
            }],
            "catalog_digest": sha(),
        });
        let catalog_text = sign_control_document(catalog_value, "catalog_digest");
        let parsed_catalog = parse_catalog_control(&catalog_text, "v2/catalog.json")
            .unwrap()
            .unwrap();
        let entry = validate_catalog(&parsed_catalog, "v2/catalog.json").unwrap();
        validate_release(&parsed_release, entry).unwrap();
    }

    #[test]
    fn signed_unavailable_catalog_is_production_only_and_exact() {
        let unavailable = json!({
            "schema": UNAVAILABLE_CATALOG_SCHEMA,
            "generated_at": "2026-07-28T00:00:00+00:00",
            "previous_catalog_sha256": sha(),
            "reason": UNAVAILABLE_REASON,
            "catalog_digest": sha(),
        });
        let text = sign_control_document(unavailable.clone(), "catalog_digest");
        assert!(parse_catalog_control(&text, "v2/catalog.json")
            .unwrap()
            .is_none());
        assert!(
            parse_catalog_control(&text, "smoketest-v2/run-1/catalog.json")
                .unwrap_err()
                .contains("only in production")
        );

        let mut extra = unavailable.clone();
        extra["extra"] = Value::Bool(true);
        let text = sign_control_document(extra, "catalog_digest");
        assert!(parse_catalog_control(&text, "v2/catalog.json")
            .unwrap_err()
            .contains("unsupported unavailable"));

        let mut bad_previous = unavailable;
        bad_previous["previous_catalog_sha256"] = Value::String("bad".into());
        let text = sign_control_document(bad_previous, "catalog_digest");
        assert!(parse_catalog_control(&text, "v2/catalog.json")
            .unwrap_err()
            .contains("unsupported unavailable"));
    }

    #[test]
    fn raw_control_digest_gate_rejects_tampering_random_digest_and_non_ascii() {
        let signed = sign_control_document(release_value(), "release_digest");
        let mut tampered: Value = serde_json::from_str(&signed).unwrap();
        tampered["legacy_core"]["entrypoints"]["forward"] =
            Value::String("2026-07-18.0/changed.json".into());
        assert!(parse_verified_control_document::<V2Release>(
            &serde_json::to_string(&tampered).unwrap(),
            "release_digest",
            "tampered release",
        )
        .unwrap_err()
        .contains("differs from its contents"));

        let mut random = release_value();
        random["release_digest"] = Value::String("f".repeat(64));
        assert!(parse_verified_control_document::<V2Release>(
            &serde_json::to_string(&random).unwrap(),
            "release_digest",
            "random release",
        )
        .unwrap_err()
        .contains("differs from its contents"));

        let mut non_ascii = release_value();
        non_ascii["families"]["places"]["coverage"]["name"] = Value::String("wörld".into());
        non_ascii["release_digest"] = Value::String("f".repeat(64));
        assert!(parse_verified_control_document::<V2Release>(
            &serde_json::to_string(&non_ascii).unwrap(),
            "release_digest",
            "non-ASCII release",
        )
        .unwrap_err()
        .contains("printable ASCII"));
    }

    #[test]
    fn catalog_validation_pins_latest_and_canonical_manifest_path() {
        assert_eq!(
            validate_catalog(&catalog(), "v2/catalog.json")
                .unwrap()
                .geocoder_build,
            "2026-07-19.1"
        );
        let mut bad = catalog();
        bad.releases[0].manifest_key = "../release.json".into();
        assert!(validate_catalog(&bad, "v2/catalog.json").is_err());

        let mut preview = catalog();
        preview.releases[0].manifest_key = "smoketest-v2/run-29705861699-1/release.json".into();
        assert!(validate_catalog(&preview, "smoketest-v2/run-29705861699-1/catalog.json").is_ok());
        assert!(validate_catalog(&preview, "v2/catalog.json").is_err());

        preview.releases.push(CatalogEntry {
            geocoder_build: "2026-07-19.0".into(),
            overture_release: "2026-06-17.0".into(),
            manifest_key: "smoketest-v2/run-29705861699-1/release.json".into(),
            manifest_sha256: sha(),
            release_digest: sha(),
        });
        assert!(validate_catalog(&preview, "smoketest-v2/run-29705861699-1/catalog.json").is_err());
    }

    #[test]
    fn release_validation_accepts_only_capabilities_the_worker_can_serve() {
        validate_release(&release(), &catalog().releases[0]).unwrap();

        let mut reverse = release_value();
        for family in ["places", "addresses"] {
            reverse["families"][family]["operations"]
                .as_array_mut()
                .unwrap()
                .push(json!("reverse"));
            reverse["families"][family]["entrypoints"]["reverse"] = json!({
                "object_key": format!(
                    "slice-2026-07-19.0/families/{family}/reverse-catalog.rcat"
                ),
                "bytes": MAX_REVERSE_CATALOG_OBJECT_BYTES,
                "sha256": sha(),
            });
        }
        reverse["operations"]["reverse"] = json!(["addresses", "divisions", "places"]);
        let reverse: V2Release = serde_json::from_value(reverse).unwrap();
        validate_release(&reverse, &catalog().releases[0]).unwrap();

        let mut malformed_reverse = reverse;
        malformed_reverse
            .families
            .get_mut("places")
            .unwrap()
            .entrypoints
            .get_mut("reverse")
            .unwrap()
            .bytes -= 1;
        assert!(validate_release(&malformed_reverse, &catalog().releases[0])
            .unwrap_err()
            .contains(CAPABILITY_INVALID_SENTINEL));

        let mut external_reverse = release_value();
        let family = "places";
        let version = "slice-2026-07-29.0";
        let request_sha256 = "b".repeat(64);
        let claim_payload = format!(
            concat!(
                "{{\"family\":\"{}\",\"overture_release\":\"2026-06-17.0\",",
                "\"request_sha256\":\"{}\",",
                "\"schema\":\"overture-construction-slice-claim-v1\",",
                "\"version\":\"{}\"}}\n"
            ),
            family, request_sha256, version
        );
        external_reverse["families"][family]["operations"]
            .as_array_mut()
            .unwrap()
            .push(json!("reverse"));
        external_reverse["families"][family]["entrypoints"]["reverse"] = json!({
            "object_key": format!(
                "{version}/families/{family}/reverse-catalog.rcat"
            ),
            "bytes": MAX_REVERSE_CATALOG_OBJECT_BYTES,
            "sha256": sha(),
        });
        external_reverse["families"][family]["operation_sources"] = json!({
            "reverse": {
                "kind": "reverse_slice",
                "version": version,
                "request_sha256": request_sha256,
                "slice_claim": {
                    "object_key": format!("{version}/claims/{family}.json"),
                    "bytes": claim_payload.len(),
                    "sha256": format!("{:x}", Sha256::digest(claim_payload.as_bytes())),
                }
            }
        });
        external_reverse["operations"]["reverse"] = json!(["divisions", "places"]);
        let external_reverse: V2Release = serde_json::from_value(external_reverse).unwrap();
        validate_release(&external_reverse, &catalog().releases[0]).unwrap();
        let places = &external_reverse.families["places"];
        assert_eq!(places.operation_version("forward"), "slice-2026-07-19.0");
        assert_eq!(places.operation_version("reverse"), version);
        assert!(release_readiness_objects(&external_reverse)
            .iter()
            .any(|object| object.key == format!("{version}/claims/{family}.json")));

        let mut bad_external = external_reverse;
        bad_external
            .families
            .get_mut(family)
            .unwrap()
            .operation_sources
            .get_mut("reverse")
            .unwrap()
            .slice_claim
            .sha256 = sha();
        assert!(validate_release(&bad_external, &catalog().releases[0])
            .unwrap_err()
            .contains(CAPABILITY_INVALID_SENTINEL));

        let mut bad_format = release();
        bad_format
            .families
            .get_mut("places")
            .unwrap()
            .versions
            .format = "PCSH9999".into();
        assert!(validate_release(&bad_format, &catalog().releases[0]).is_err());

        let mut unsupported_operation = release();
        let places = unsupported_operation.families.get_mut("places").unwrap();
        places.operations = vec!["reverse".into()];
        places.entrypoints = HashMap::from([(
            "reverse".into(),
            ArtifactIdentity {
                object_key: "slice-2026-07-19.0/families/places/catalog.pcat".into(),
                bytes: 123,
                sha256: sha(),
            },
        )]);
        assert!(validate_release(&unsupported_operation, &catalog().releases[0]).is_err());

        let mut oversized_entrypoint = release();
        oversized_entrypoint
            .families
            .get_mut("places")
            .unwrap()
            .entrypoints
            .get_mut("forward")
            .unwrap()
            .bytes = MAX_CATALOG_OBJECT_BYTES + 1;
        assert!(validate_release(&oversized_entrypoint, &catalog().releases[0]).is_err());
    }

    #[test]
    fn readiness_gate_requires_completion_markers_and_places_head() {
        let mut release = release();
        let family_manifest = serde_json::to_string(&json!({
            "schema": FAMILY_MANIFEST_SCHEMA,
            "family": "places",
            "manifest_digest": sha(),
            "artifacts": [{
                "object_key": PLACES_HEAD_ARTIFACT_KEY,
                "bytes": 456,
                "sha256": "c".repeat(64)
            }]
        }))
        .unwrap();
        release.families.get_mut("places").unwrap().manifest_sha256 =
            format!("{:x}", Sha256::digest(family_manifest.as_bytes()));
        let requirements = release_readiness_objects(&release);
        let keys = requirements
            .iter()
            .map(|requirement| requirement.key.as_str())
            .collect::<HashSet<_>>();
        assert!(keys.contains("2026-07-18.0/release-manifest.json"));
        assert!(keys.contains("slice-2026-07-19.0/slice-manifest.json"));
        assert!(keys.contains("slice-2026-07-19.0/families/addresses/family-manifest.json"));
        assert!(!keys.contains("slice-2026-07-19.0/families/places/head.phrp"));
        assert_eq!(
            requirements
                .iter()
                .find(|requirement| requirement.key.ends_with("catalog.pcat"))
                .unwrap()
                .expected_bytes,
            Some(123)
        );
        assert_eq!(
            requirements
                .iter()
                .find(|requirement| requirement.key.ends_with("catalog.pcat"))
                .unwrap()
                .expected_sha256
                .clone(),
            sha()
        );
        assert_eq!(
            requirements
                .iter()
                .filter(|requirement| requirement.key.ends_with("slice-manifest.json"))
                .count(),
            1
        );

        let catalog_requirement = requirements
            .iter()
            .find(|requirement| requirement.key.ends_with("catalog.pcat"))
            .unwrap();
        assert!(readiness_identity_matches(catalog_requirement, 123, &sha()));
        assert!(!readiness_identity_matches(
            catalog_requirement,
            123,
            &"b".repeat(64)
        ));

        let head =
            places_head_requirement(&family_manifest, release.families.get("places").unwrap())
                .unwrap();
        assert_eq!(head.key, "slice-2026-07-19.0/families/places/head.phrp");
        assert_eq!(head.expected_bytes, Some(456));
        assert_eq!(head.expected_sha256, "c".repeat(64));
        assert!(readiness_identity_matches(&head, 456, &"c".repeat(64)));
        assert!(!readiness_identity_matches(&head, 455, &"c".repeat(64)));
        assert!(!readiness_identity_matches(&head, 456, &"d".repeat(64)));
    }

    #[test]
    fn places_head_extraction_rejects_tampered_missing_or_duplicate_identity() {
        let mut release = release();
        let places = release.families.get_mut("places").unwrap();
        let manifest = |artifacts: Value| {
            serde_json::to_string(&json!({
                "schema": FAMILY_MANIFEST_SCHEMA,
                "family": "places",
                "manifest_digest": sha(),
                "artifacts": artifacts
            }))
            .unwrap()
        };
        let valid_artifact = json!({
            "object_key": PLACES_HEAD_ARTIFACT_KEY,
            "bytes": 456,
            "sha256": "c".repeat(64)
        });

        let missing = manifest(json!([{
            "object_key": "families/places/not-head.phrp",
            "bytes": 456,
            "sha256": "c".repeat(64)
        }]));
        places.manifest_sha256 = format!("{:x}", Sha256::digest(missing.as_bytes()));
        assert!(places_head_requirement(&missing, places).is_err());

        let duplicate = manifest(json!([valid_artifact.clone(), valid_artifact.clone()]));
        places.manifest_sha256 = format!("{:x}", Sha256::digest(duplicate.as_bytes()));
        assert!(places_head_requirement(&duplicate, places).is_err());

        let valid = manifest(json!([valid_artifact]));
        places.manifest_sha256 = "f".repeat(64);
        assert!(places_head_requirement(&valid, places).is_err());
    }

    fn construction_release_value() -> Value {
        let mut value = release_value();
        value["families"]["places"]["versions"]["format"] =
            json!(crate::places_construction_v1::PLACES_CONSTRUCTION_FORMAT);
        value["families"]["places"]["entrypoints"]["forward"]["object_key"] =
            json!("slice-2026-07-19.0/families/places/routing.json");
        value
    }

    #[test]
    fn construction_format_is_accepted_for_slices_only_with_routing_entrypoint() {
        let release: V2Release = serde_json::from_value(construction_release_value()).unwrap();
        validate_release(&release, &catalog().releases[0]).unwrap();

        // Construction format with the PCSH catalog entrypoint fails closed.
        let mut mixed = construction_release_value();
        mixed["families"]["places"]["entrypoints"]["forward"]["object_key"] =
            json!("slice-2026-07-19.0/families/places/catalog.pcat");
        let mixed: V2Release = serde_json::from_value(mixed).unwrap();
        assert!(validate_release(&mixed, &catalog().releases[0]).is_err());

        // PCSH format with the routing entrypoint fails closed.
        let mut mixed = release_value();
        mixed["families"]["places"]["entrypoints"]["forward"]["object_key"] =
            json!("slice-2026-07-19.0/families/places/routing.json");
        let mixed: V2Release = serde_json::from_value(mixed).unwrap();
        assert!(validate_release(&mixed, &catalog().releases[0]).is_err());

        // Construction format on a core_release source fails closed.
        let mut core = construction_release_value();
        core["families"]["places"]["source"] = json!({
            "kind": "core_release",
            "version": "2026-07-19.0",
            "manifest_key": "2026-07-19.0/release-manifest.json",
            "manifest_sha256": sha(),
        });
        core["families"]["places"]["manifest_key"] =
            json!("2026-07-19.0/families/places/family-manifest.json");
        core["families"]["places"]["entrypoints"]["forward"]["object_key"] =
            json!("2026-07-19.0/families/places/routing.json");
        let core: V2Release = serde_json::from_value(core).unwrap();
        assert!(validate_release(&core, &catalog().releases[0]).is_err());

        // An unknown format string stays rejected on either source kind.
        let mut unknown = construction_release_value();
        unknown["families"]["places"]["versions"]["format"] = json!("PLRV9999+PLHD9999");
        let unknown: V2Release = serde_json::from_value(unknown).unwrap();
        assert!(validate_release(&unknown, &catalog().releases[0]).is_err());

        // The routing entrypoint keeps its own byte cap.
        let mut oversized = construction_release_value();
        oversized["families"]["places"]["entrypoints"]["forward"]["bytes"] =
            json!(MAX_PLACES_ROUTING_BYTES + 1);
        let oversized: V2Release = serde_json::from_value(oversized).unwrap();
        assert!(validate_release(&oversized, &catalog().releases[0]).is_err());
    }

    fn hex_name(seed: u8, extension: &str) -> String {
        format!("{}{extension}", format!("{seed:02x}").repeat(32))
    }

    struct ConstructionFixture {
        family: FamilyReference,
        manifest_text: String,
        routing_text: String,
        head_text: String,
        shard_identities: Vec<(u32, String, String, u64)>,
    }

    /// A consistent promoted-slice control-document set: three populated head
    /// shards, one unsplit routed cell, and a family manifest attesting every
    /// object plus routing.json, with the release entrypoint pinned to the
    /// routing bytes.
    fn construction_fixture() -> ConstructionFixture {
        let routed_name = hex_name(0x11, ".plrv");
        let shard_identities: Vec<(u32, String, String, u64)> = [3_u32, 7, 12]
            .into_iter()
            .map(|id| {
                (
                    id,
                    hex_name(0x20 + id as u8, ".plhd"),
                    format!("{:02x}", 0x40 + id).repeat(32),
                    100 + u64::from(id),
                )
            })
            .collect();
        let head_text = serde_json::to_string(&json!({
            "schema": "overture-places-global-head-sharded-v2",
            "shard_bits": 4,
            "shard_count": 16,
            "populated_shards": 3,
            "shards": shard_identities
                .iter()
                .map(|(id, path, sha256, bytes)| json!({
                    "shard_id": id,
                    "path": path,
                    "sha256": sha256,
                    "bytes": bytes,
                }))
                .collect::<Vec<_>>(),
        }))
        .unwrap();
        let head_name = format!("{:x}.json", Sha256::digest(head_text.as_bytes()));
        let routing_text = serde_json::to_string(&json!({
            "schema": "overture-promoted-places-routing-v1",
            "family": "places",
            "cell_scheme": "level-4-quadkey-yx-hex",
            "subpartition_scheme": "token-sha256-nibble-prefix-v1",
            "cells": {"8080": [["", routed_name]]},
            "head": {
                "schema": "overture-places-global-head-sharded-v2",
                "shard_bits": 4,
                "shard_count": 16,
                "populated_shards": 3,
                "manifest_object": head_name,
            },
        }))
        .unwrap();
        let mut artifacts = vec![json!({
            "object_key": "families/places/routing.json",
            "bytes": routing_text.len(),
            "sha256": format!("{:x}", Sha256::digest(routing_text.as_bytes())),
        })];
        artifacts.push(json!({
            "object_key": format!("families/places/objects/{routed_name}"),
            "bytes": 4096,
            "sha256": "1".repeat(64),
        }));
        artifacts.push(json!({
            "object_key": format!("families/places/objects/{head_name}"),
            "bytes": head_text.len(),
            "sha256": format!("{:x}", Sha256::digest(head_text.as_bytes())),
        }));
        for (_, path, sha256, bytes) in &shard_identities {
            artifacts.push(json!({
                "object_key": format!("families/places/objects/{path}"),
                "bytes": bytes,
                "sha256": sha256,
            }));
        }
        let manifest_text = serde_json::to_string(&json!({
            "schema": FAMILY_MANIFEST_SCHEMA,
            "family": "places",
            "manifest_digest": sha(),
            "artifacts": artifacts,
        }))
        .unwrap();
        let mut family = construction_release_value()["families"]["places"].clone();
        family["manifest_sha256"] =
            json!(format!("{:x}", Sha256::digest(manifest_text.as_bytes())));
        family["entrypoints"]["forward"]["bytes"] = json!(routing_text.len());
        family["entrypoints"]["forward"]["sha256"] =
            json!(format!("{:x}", Sha256::digest(routing_text.as_bytes())));
        let family: FamilyReference = serde_json::from_value(family).unwrap();
        ConstructionFixture {
            family,
            manifest_text,
            routing_text,
            head_text,
            shard_identities,
        }
    }

    #[test]
    fn construction_admission_pins_identities_and_samples_head_shards() {
        let fixture = construction_fixture();
        let (artifacts, routing) = places_construction_admission(
            &fixture.manifest_text,
            &fixture.routing_text,
            &fixture.family,
        )
        .unwrap();
        let requirements = places_construction_head_requirements(
            &fixture.head_text,
            &routing,
            &artifacts,
            "slice-2026-07-19.0",
        )
        .unwrap();

        // The sample is the first, middle, and last populated shards.
        let expected: Vec<&(u32, String, String, u64)> = vec![
            &fixture.shard_identities[0],
            &fixture.shard_identities[1],
            &fixture.shard_identities[2],
        ];
        assert_eq!(requirements.len(), expected.len());
        for (requirement, (_, path, sha256, bytes)) in requirements.iter().zip(expected) {
            assert_eq!(
                requirement.key,
                format!("slice-2026-07-19.0/families/places/objects/{path}")
            );
            assert_eq!(requirement.expected_bytes, Some(*bytes));
            assert_eq!(&requirement.expected_sha256, sha256);
            // The streamed shard must reproduce the manifest identity exactly:
            // a tampered stored shard (wrong bytes or wrong hash) fails.
            assert!(readiness_identity_matches(requirement, *bytes, sha256));
            assert!(!readiness_identity_matches(requirement, *bytes + 1, sha256));
            assert!(!readiness_identity_matches(
                requirement,
                *bytes,
                &"f".repeat(64)
            ));
        }
    }

    #[test]
    fn construction_admission_requires_reverse_root_manifest_attestation() {
        let mut fixture = construction_fixture();
        let mut artifacts =
            family_manifest_artifacts(&fixture.manifest_text, &fixture.family, "places").unwrap();
        let reverse_sha = "a".repeat(64);
        let reverse = ArtifactIdentity {
            object_key: "slice-2026-07-19.0/families/places/reverse-catalog.rcat".to_string(),
            bytes: MAX_REVERSE_CATALOG_OBJECT_BYTES,
            sha256: reverse_sha.clone(),
        };
        fixture
            .family
            .entrypoints
            .insert("reverse".to_string(), reverse);

        assert!(
            attest_family_entrypoints(&artifacts, &fixture.family, "places")
                .unwrap_err()
                .contains("omits its reverse entrypoint")
        );

        artifacts.insert(
            "families/places/reverse-catalog.rcat".to_string(),
            (MAX_REVERSE_CATALOG_OBJECT_BYTES as u64, reverse_sha),
        );
        attest_family_entrypoints(&artifacts, &fixture.family, "places").unwrap();

        artifacts
            .get_mut("families/places/reverse-catalog.rcat")
            .unwrap()
            .0 -= 1;
        assert!(
            attest_family_entrypoints(&artifacts, &fixture.family, "places")
                .unwrap_err()
                .contains("identity differs")
        );
    }

    #[test]
    fn construction_admission_rejects_tampered_documents() {
        let fixture = construction_fixture();

        // Tampered family manifest bytes.
        let mut tampered_manifest = fixture.manifest_text.clone();
        tampered_manifest.push(' ');
        assert!(places_construction_admission(
            &tampered_manifest,
            &fixture.routing_text,
            &fixture.family
        )
        .unwrap_err()
        .contains("SHA-256 differs"));

        // Tampered routing bytes.
        let mut tampered_routing = fixture.routing_text.clone();
        tampered_routing.push(' ');
        assert!(places_construction_admission(
            &fixture.manifest_text,
            &tampered_routing,
            &fixture.family
        )
        .unwrap_err()
        .contains("release-pinned identity"));

        // A routed object the family manifest does not attest.
        let mut unattested: Value = serde_json::from_str(&fixture.manifest_text).unwrap();
        let artifacts = unattested["artifacts"].as_array_mut().unwrap();
        artifacts.retain(|artifact| !artifact["object_key"].as_str().unwrap().ends_with(".plrv"));
        let unattested = serde_json::to_string(&unattested).unwrap();
        let mut family = construction_release_value()["families"]["places"].clone();
        family["manifest_sha256"] = json!(format!("{:x}", Sha256::digest(unattested.as_bytes())));
        family["entrypoints"]["forward"]["bytes"] = json!(fixture.routing_text.len());
        family["entrypoints"]["forward"]["sha256"] = json!(format!(
            "{:x}",
            Sha256::digest(fixture.routing_text.as_bytes())
        ));
        let family: FamilyReference = serde_json::from_value(family).unwrap();
        assert!(
            places_construction_admission(&unattested, &fixture.routing_text, &family)
                .unwrap_err()
                .contains("unattested object")
        );

        // Tampered head routing manifest bytes.
        let (artifacts, routing) = places_construction_admission(
            &fixture.manifest_text,
            &fixture.routing_text,
            &fixture.family,
        )
        .unwrap();
        let mut tampered_head = fixture.head_text.clone();
        tampered_head.push(' ');
        assert!(places_construction_head_requirements(
            &tampered_head,
            &routing,
            &artifacts,
            "slice-2026-07-19.0"
        )
        .unwrap_err()
        .contains("differ from attestation"));

        // A shard whose identity disagrees between the two manifests.
        let mut lying_head: Value = serde_json::from_str(&fixture.head_text).unwrap();
        lying_head["shards"][0]["sha256"] = json!("e".repeat(64));
        let lying_head = serde_json::to_string(&lying_head).unwrap();
        let mut manifest: Value = serde_json::from_str(&fixture.manifest_text).unwrap();
        for artifact in manifest["artifacts"].as_array_mut().unwrap() {
            let key = artifact["object_key"].as_str().unwrap().to_string();
            if key.ends_with(".json") && key.contains("objects/") {
                artifact["bytes"] = json!(lying_head.len());
                artifact["sha256"] = json!(format!("{:x}", Sha256::digest(lying_head.as_bytes())));
            }
        }
        // The head manifest object is content-addressed, so its renamed copy
        // needs a matching routing pointer; patch both.
        let lying_head_name = format!("{:x}.json", Sha256::digest(lying_head.as_bytes()));
        for artifact in manifest["artifacts"].as_array_mut().unwrap() {
            let key = artifact["object_key"].as_str().unwrap().to_string();
            if key.ends_with(".json") && key.contains("objects/") {
                artifact["object_key"] =
                    json!(format!("families/places/objects/{lying_head_name}"));
            }
        }
        let mut routing_value: Value = serde_json::from_str(&fixture.routing_text).unwrap();
        routing_value["head"]["manifest_object"] = json!(lying_head_name);
        let routing_text = serde_json::to_string(&routing_value).unwrap();
        for artifact in manifest["artifacts"].as_array_mut().unwrap() {
            if artifact["object_key"] == json!("families/places/routing.json") {
                artifact["bytes"] = json!(routing_text.len());
                artifact["sha256"] =
                    json!(format!("{:x}", Sha256::digest(routing_text.as_bytes())));
            }
        }
        let manifest_text = serde_json::to_string(&manifest).unwrap();
        let mut family = construction_release_value()["families"]["places"].clone();
        family["manifest_sha256"] =
            json!(format!("{:x}", Sha256::digest(manifest_text.as_bytes())));
        family["entrypoints"]["forward"]["bytes"] = json!(routing_text.len());
        family["entrypoints"]["forward"]["sha256"] =
            json!(format!("{:x}", Sha256::digest(routing_text.as_bytes())));
        let family: FamilyReference = serde_json::from_value(family).unwrap();
        let (artifacts, routing) =
            places_construction_admission(&manifest_text, &routing_text, &family).unwrap();
        assert!(places_construction_head_requirements(
            &lying_head,
            &routing,
            &artifacts,
            "slice-2026-07-19.0"
        )
        .unwrap_err()
        .contains("disagrees between manifests"));

        // Head geometry that differs from routing.json fails closed.
        let mut short_routing: Value = serde_json::from_str(&fixture.routing_text).unwrap();
        short_routing["head"]["populated_shards"] = json!(2);
        let short_routing = serde_json::to_string(&short_routing).unwrap();
        let mut manifest: Value = serde_json::from_str(&fixture.manifest_text).unwrap();
        for artifact in manifest["artifacts"].as_array_mut().unwrap() {
            if artifact["object_key"] == json!("families/places/routing.json") {
                artifact["bytes"] = json!(short_routing.len());
                artifact["sha256"] =
                    json!(format!("{:x}", Sha256::digest(short_routing.as_bytes())));
            }
        }
        let manifest_text = serde_json::to_string(&manifest).unwrap();
        let mut family = construction_release_value()["families"]["places"].clone();
        family["manifest_sha256"] =
            json!(format!("{:x}", Sha256::digest(manifest_text.as_bytes())));
        family["entrypoints"]["forward"]["bytes"] = json!(short_routing.len());
        family["entrypoints"]["forward"]["sha256"] =
            json!(format!("{:x}", Sha256::digest(short_routing.as_bytes())));
        let family: FamilyReference = serde_json::from_value(family).unwrap();
        let (artifacts, routing) =
            places_construction_admission(&manifest_text, &short_routing, &family).unwrap();
        assert!(places_construction_head_requirements(
            &fixture.head_text,
            &routing,
            &artifacts,
            "slice-2026-07-19.0"
        )
        .unwrap_err()
        .contains("geometry differs"));
    }

    fn address_construction_release_value() -> Value {
        let mut value = release_value();
        value["families"]["addresses"]["versions"]["format"] = json!(ADDRESS_CONSTRUCTION_FORMAT);
        value["families"]["addresses"]["versions"]["normalization"] =
            json!(ADDRESS_CONSTRUCTION_NORMALIZATION_VERSION);
        value["families"]["addresses"]["entrypoints"]["structured_forward"]["object_key"] =
            json!("slice-2026-07-19.0/families/addresses/routing.json");
        value
    }

    #[test]
    fn address_construction_format_is_accepted_for_slices_only_with_routing_entrypoint() {
        let release: V2Release =
            serde_json::from_value(address_construction_release_value()).unwrap();
        validate_release(&release, &catalog().releases[0]).unwrap();

        // Construction format with the reduce-2 collection entrypoint fails
        // closed.
        let mut mixed = address_construction_release_value();
        mixed["families"]["addresses"]["entrypoints"]["structured_forward"]["object_key"] =
            json!("slice-2026-07-19.0/families/addresses/address-collection.json");
        let mixed: V2Release = serde_json::from_value(mixed).unwrap();
        assert!(validate_release(&mixed, &catalog().releases[0]).is_err());

        // reduce-2 format with the routing entrypoint fails closed.
        let mut mixed = release_value();
        mixed["families"]["addresses"]["entrypoints"]["structured_forward"]["object_key"] =
            json!("slice-2026-07-19.0/families/addresses/routing.json");
        let mixed: V2Release = serde_json::from_value(mixed).unwrap();
        assert!(validate_release(&mixed, &catalog().releases[0]).is_err());

        // Construction format on a core_release source fails closed.
        let mut core = address_construction_release_value();
        core["families"]["addresses"]["source"] = json!({
            "kind": "core_release",
            "version": "2026-07-19.0",
            "manifest_key": "2026-07-19.0/release-manifest.json",
            "manifest_sha256": sha(),
        });
        core["families"]["addresses"]["manifest_key"] =
            json!("2026-07-19.0/families/addresses/family-manifest.json");
        core["families"]["addresses"]["entrypoints"]["structured_forward"]["object_key"] =
            json!("2026-07-19.0/families/addresses/routing.json");
        let core: V2Release = serde_json::from_value(core).unwrap();
        assert!(validate_release(&core, &catalog().releases[0]).is_err());

        // An unknown format string stays rejected.
        let mut unknown = address_construction_release_value();
        unknown["families"]["addresses"]["versions"]["format"] = json!("OAV1ART2");
        let unknown: V2Release = serde_json::from_value(unknown).unwrap();
        assert!(validate_release(&unknown, &catalog().releases[0]).is_err());

        // Each lane requires exactly its own normalization identity.
        let mut wrong_normalization = address_construction_release_value();
        wrong_normalization["families"]["addresses"]["versions"]["normalization"] =
            json!(ADDRESS_NORMALIZATION_VERSION);
        let wrong_normalization: V2Release = serde_json::from_value(wrong_normalization).unwrap();
        assert!(validate_release(&wrong_normalization, &catalog().releases[0]).is_err());
        let mut wrong_normalization = release_value();
        wrong_normalization["families"]["addresses"]["versions"]["normalization"] =
            json!(ADDRESS_CONSTRUCTION_NORMALIZATION_VERSION);
        let wrong_normalization: V2Release = serde_json::from_value(wrong_normalization).unwrap();
        assert!(validate_release(&wrong_normalization, &catalog().releases[0]).is_err());

        // A construction family must not grow a tokenizer.
        let mut tokenized = address_construction_release_value();
        tokenized["families"]["addresses"]["versions"]["tokenizer"] = json!("t1");
        let tokenized: V2Release = serde_json::from_value(tokenized).unwrap();
        assert!(validate_release(&tokenized, &catalog().releases[0]).is_err());

        // The routing entrypoint keeps its own byte cap.
        let mut oversized = address_construction_release_value();
        oversized["families"]["addresses"]["entrypoints"]["structured_forward"]["bytes"] =
            json!(MAX_ADDRESS_ROUTING_BYTES + 1);
        let oversized: V2Release = serde_json::from_value(oversized).unwrap();
        assert!(validate_release(&oversized, &catalog().releases[0]).is_err());
    }

    #[test]
    fn address_construction_readiness_skips_the_bounded_parse_manifest() {
        // The reduce-2 addresses manifest is streamed as a plain readiness
        // object (asserted by the readiness gate test above); the construction
        // manifest is loaded under a bounded parse instead, exactly like the
        // Places manifest, so it must not appear twice.
        let release: V2Release =
            serde_json::from_value(address_construction_release_value()).unwrap();
        let keys: HashSet<String> = release_readiness_objects(&release)
            .into_iter()
            .map(|requirement| requirement.key)
            .collect();
        assert!(!keys.contains("slice-2026-07-19.0/families/addresses/family-manifest.json"));
        assert!(keys.contains("slice-2026-07-19.0/families/addresses/routing.json"));
    }

    struct AddressConstructionFixture {
        family: FamilyReference,
        manifest_text: String,
        routing_text: String,
        smaller: String,
        small: String,
        large: String,
    }

    /// A consistent promoted-slice address control-document set: two small
    /// routed objects, one above the admission sample cap, and a family
    /// manifest attesting every object plus routing.json, with the release
    /// entrypoint pinned to the routing bytes.
    fn address_construction_fixture() -> AddressConstructionFixture {
        let smaller = hex_name(0x32, ".av1");
        let small = hex_name(0x31, ".av1");
        let large = hex_name(0x33, ".av1");
        let routing_text = serde_json::to_string(&json!({
            "schema": "overture-promoted-addresses-routing-v1",
            "family": "addresses",
            "key_scheme": "country-route-hash-range-v1",
            "partitions": [
                {"country": "mc", "hash_start": 0_u64, "hash_end": u64::MAX, "object": smaller},
                {"country": "us", "hash_start": 0_u64, "hash_end": 999_u64, "object": small},
                {"country": "us", "hash_start": 1000_u64, "hash_end": u64::MAX, "object": large},
            ],
        }))
        .unwrap();
        let artifacts = vec![
            json!({
                "object_key": "families/addresses/routing.json",
                "bytes": routing_text.len(),
                "sha256": format!("{:x}", Sha256::digest(routing_text.as_bytes())),
            }),
            json!({
                "object_key": format!("families/addresses/objects/{smaller}"),
                "bytes": 500,
                "sha256": "2".repeat(64),
            }),
            json!({
                "object_key": format!("families/addresses/objects/{small}"),
                "bytes": 1000,
                "sha256": "1".repeat(64),
            }),
            json!({
                "object_key": format!("families/addresses/objects/{large}"),
                "bytes": MAX_ADDRESS_ADMISSION_SAMPLE_BYTES + 1,
                "sha256": "3".repeat(64),
            }),
        ];
        let manifest_text = serde_json::to_string(&json!({
            "schema": FAMILY_MANIFEST_SCHEMA,
            "family": "addresses",
            "manifest_digest": sha(),
            "artifacts": artifacts,
        }))
        .unwrap();
        let mut family = address_construction_release_value()["families"]["addresses"].clone();
        family["manifest_sha256"] =
            json!(format!("{:x}", Sha256::digest(manifest_text.as_bytes())));
        family["entrypoints"]["structured_forward"]["bytes"] = json!(routing_text.len());
        family["entrypoints"]["structured_forward"]["sha256"] =
            json!(format!("{:x}", Sha256::digest(routing_text.as_bytes())));
        let family: FamilyReference = serde_json::from_value(family).unwrap();
        AddressConstructionFixture {
            family,
            manifest_text,
            routing_text,
            smaller,
            small,
            large,
        }
    }

    #[test]
    fn address_admission_pins_identities_and_samples_smallest_objects() {
        let fixture = address_construction_fixture();
        let (artifacts, routing) = address_construction_admission(
            &fixture.manifest_text,
            &fixture.routing_text,
            &fixture.family,
        )
        .unwrap();
        let sample =
            address_construction_sample(&routing, &artifacts, "slice-2026-07-19.0").unwrap();
        // The over-cap object is excluded; the rest are streamed smallest
        // first with their attested identities.
        assert_eq!(sample.len(), 2);
        assert_eq!(
            sample[0].key,
            format!(
                "slice-2026-07-19.0/families/addresses/objects/{}",
                fixture.smaller
            )
        );
        assert_eq!(sample[0].expected_bytes, Some(500));
        assert_eq!(sample[0].expected_sha256, "2".repeat(64));
        assert_eq!(
            sample[1].key,
            format!(
                "slice-2026-07-19.0/families/addresses/objects/{}",
                fixture.small
            )
        );
        assert_eq!(sample[1].expected_bytes, Some(1000));
        assert!(!sample
            .iter()
            .any(|requirement| requirement.key.contains(&fixture.large)));
        // A tampered stored object (wrong bytes or wrong hash) fails.
        assert!(readiness_identity_matches(&sample[0], 500, &"2".repeat(64)));
        assert!(!readiness_identity_matches(
            &sample[0],
            501,
            &"2".repeat(64)
        ));
        assert!(!readiness_identity_matches(
            &sample[0],
            500,
            &"f".repeat(64)
        ));
    }

    #[test]
    fn address_admission_rejects_tampered_documents() {
        let fixture = address_construction_fixture();

        // Tampered family manifest bytes.
        let mut tampered_manifest = fixture.manifest_text.clone();
        tampered_manifest.push(' ');
        assert!(address_construction_admission(
            &tampered_manifest,
            &fixture.routing_text,
            &fixture.family
        )
        .unwrap_err()
        .contains("SHA-256 differs"));

        // Tampered routing bytes.
        let mut tampered_routing = fixture.routing_text.clone();
        tampered_routing.push(' ');
        assert!(address_construction_admission(
            &fixture.manifest_text,
            &tampered_routing,
            &fixture.family
        )
        .unwrap_err()
        .contains("release-pinned identity"));

        // A routed object the family manifest does not attest.
        let mut unattested: Value = serde_json::from_str(&fixture.manifest_text).unwrap();
        let artifacts = unattested["artifacts"].as_array_mut().unwrap();
        artifacts.retain(|artifact| {
            !artifact["object_key"]
                .as_str()
                .unwrap()
                .contains(&fixture.small)
        });
        let unattested = serde_json::to_string(&unattested).unwrap();
        let mut family = address_construction_release_value()["families"]["addresses"].clone();
        family["manifest_sha256"] = json!(format!("{:x}", Sha256::digest(unattested.as_bytes())));
        family["entrypoints"]["structured_forward"]["bytes"] = json!(fixture.routing_text.len());
        family["entrypoints"]["structured_forward"]["sha256"] = json!(format!(
            "{:x}",
            Sha256::digest(fixture.routing_text.as_bytes())
        ));
        let family: FamilyReference = serde_json::from_value(family).unwrap();
        assert!(
            address_construction_admission(&unattested, &fixture.routing_text, &family)
                .unwrap_err()
                .contains("unattested object")
        );

        // A family manifest whose routing attestation disagrees with the
        // release-pinned entrypoint identity.
        let mut lying: Value = serde_json::from_str(&fixture.manifest_text).unwrap();
        for artifact in lying["artifacts"].as_array_mut().unwrap() {
            if artifact["object_key"] == json!("families/addresses/routing.json") {
                artifact["sha256"] = json!("e".repeat(64));
            }
        }
        let lying = serde_json::to_string(&lying).unwrap();
        let mut family = address_construction_release_value()["families"]["addresses"].clone();
        family["manifest_sha256"] = json!(format!("{:x}", Sha256::digest(lying.as_bytes())));
        family["entrypoints"]["structured_forward"]["bytes"] = json!(fixture.routing_text.len());
        family["entrypoints"]["structured_forward"]["sha256"] = json!(format!(
            "{:x}",
            Sha256::digest(fixture.routing_text.as_bytes())
        ));
        let family: FamilyReference = serde_json::from_value(family).unwrap();
        assert!(
            address_construction_admission(&lying, &fixture.routing_text, &family)
                .unwrap_err()
                .contains("differs between manifest and release")
        );

        // A manifest claiming the wrong family name.
        let mut wrong_family: Value = serde_json::from_str(&fixture.manifest_text).unwrap();
        wrong_family["family"] = json!("places");
        let wrong_family = serde_json::to_string(&wrong_family).unwrap();
        let mut family = address_construction_release_value()["families"]["addresses"].clone();
        family["manifest_sha256"] = json!(format!("{:x}", Sha256::digest(wrong_family.as_bytes())));
        family["entrypoints"]["structured_forward"]["bytes"] = json!(fixture.routing_text.len());
        family["entrypoints"]["structured_forward"]["sha256"] = json!(format!(
            "{:x}",
            Sha256::digest(fixture.routing_text.as_bytes())
        ));
        let family: FamilyReference = serde_json::from_value(family).unwrap();
        assert!(
            address_construction_admission(&wrong_family, &fixture.routing_text, &family)
                .unwrap_err()
                .contains("unsupported v2 addresses family manifest")
        );
    }

    #[test]
    fn type_parser_accepts_public_poi_aliases_and_rejects_unknowns() {
        let raw = "place,neighbourhood".to_string();
        let parsed = parse_types(Some(&raw)).unwrap();
        assert_eq!(parsed, HashSet::from(["poi".into(), "neighborhood".into()]));
        assert!(parse_types(Some(&"planet".to_string())).is_err());
    }

    #[test]
    fn reverse_radius_and_limit_defaults_are_family_specific_and_bounded() {
        assert_eq!(parse_reverse_limit(None).unwrap(), 1);
        assert_eq!(
            parse_reverse_radius(None, ReverseFamily::Places).unwrap(),
            250
        );
        assert_eq!(
            parse_reverse_radius(None, ReverseFamily::Addresses).unwrap(),
            100
        );
        assert!(parse_reverse_radius(Some(&"501".into()), ReverseFamily::Addresses).is_err());
        assert!(parse_reverse_radius(Some(&"0".into()), ReverseFamily::Places).is_err());
        assert!(parse_reverse_limit(Some(&"11".into())).is_err());
    }

    #[test]
    fn structured_address_aliases_fill_the_exact_key_contract() {
        let params = HashMap::from([
            ("country".into(), "US".into()),
            ("state".into(), "MA".into()),
            ("city".into(), "Stoneham".into()),
            ("postcode".into(), "02180".into()),
            ("street".into(), "Main Street".into()),
            ("address_number".into(), "10".into()),
        ]);
        let mapped = address_params(&params);
        assert_eq!(mapped.len(), crate::address::FIELD_NAMES.len());
        let key = build_lookup_key(&mapped).unwrap();
        assert_eq!(key[0], "us");
        assert_eq!(key[1], "ma");
        assert_eq!(key[2], "");
        assert_eq!(key[6], "10");
    }

    #[test]
    fn structured_address_state_city_aliases_plan_measured_source_encodings() {
        let params = HashMap::from([
            ("country".into(), "US".into()),
            ("state".into(), " WA ".into()),
            ("city".into(), " Seattle ".into()),
            ("postcode".into(), "98109".into()),
            ("street".into(), "Broad Street".into()),
            ("number".into(), "400".into()),
        ]);
        let plan = address_lookup_plan(&params).unwrap();
        assert_eq!(
            plan.iter().map(|item| item.variant).collect::<Vec<_>>(),
            vec![
                "address_level_city",
                "postal_city",
                "address_level_and_postal_city"
            ]
        );
        assert_eq!(
            plan.iter()
                .map(|item| item.key[1..4].to_vec())
                .collect::<Vec<_>>(),
            vec![
                ["wa", "seattle", ""].map(str::to_string).to_vec(),
                ["wa", "", "seattle"].map(str::to_string).to_vec(),
                ["wa", "seattle", "seattle"].map(str::to_string).to_vec(),
            ]
        );
    }

    #[test]
    fn structured_address_region_city_aliases_use_the_same_bounded_plan() {
        let params = HashMap::from([
            ("country".into(), "US".into()),
            ("region".into(), "WA".into()),
            ("city".into(), "Seattle".into()),
            ("postcode".into(), "98109".into()),
            ("street".into(), "Broad Street".into()),
            ("number".into(), "400".into()),
        ]);
        let plan = address_lookup_plan(&params).unwrap();
        assert_eq!(plan.len(), 3);
        assert!(plan.iter().all(|item| item.key[1] == "wa"));
    }

    #[test]
    fn structured_address_explicit_context_keeps_one_exact_key() {
        for explicit in [
            ("admin_level_general", "WA"),
            ("admin_level_specific", "Seattle"),
            ("postal_city", ""),
        ] {
            let mut params = HashMap::from([
                ("country".into(), "US".into()),
                ("state".into(), "WA".into()),
                ("city".into(), "Seattle".into()),
                ("postcode".into(), "98109".into()),
                ("street".into(), "Broad Street".into()),
                ("number".into(), "400".into()),
            ]);
            params.insert(explicit.0.into(), explicit.1.into());
            let plan = address_lookup_plan(&params).unwrap();
            assert_eq!(plan.len(), 1);
            assert_eq!(plan[0].variant, "exact");
        }
    }

    #[test]
    fn structured_address_alias_plan_does_not_infer_or_drop_context() {
        let base = HashMap::from([
            ("country".into(), "US".into()),
            ("city".into(), "Seattle".into()),
            ("postcode".into(), "98109".into()),
            ("street".into(), "Broad Street".into()),
            ("number".into(), "400".into()),
        ]);
        let without_state = address_lookup_plan(&base).unwrap();
        assert_eq!(without_state.len(), 1);
        assert_eq!(without_state[0].variant, "exact");
        assert_eq!(without_state[0].key[1], "");

        let mut with_county = base;
        with_county.insert("state".into(), "WA".into());
        with_county.insert("county".into(), "King".into());
        let with_county = address_lookup_plan(&with_county).unwrap();
        assert_eq!(with_county.len(), 1);
        assert_eq!(with_county[0].variant, "exact");
        assert_eq!(with_county[0].key[2], "king");
    }

    #[test]
    fn structured_address_lookup_continues_only_after_a_current_empty_result() {
        let empty = AddressOutcome::Resolved {
            data_version: "build-1".into(),
            normalization_version: "normalization-1",
            candidates: Vec::new(),
        };
        assert_eq!(
            address_lookup_disposition(&empty, "build-1").unwrap(),
            AddressLookupDisposition::Continue
        );

        let matched = AddressOutcome::Resolved {
            data_version: "build-1".into(),
            normalization_version: "normalization-1",
            candidates: vec![crate::address_pages::AddressPageRecord {
                key: Default::default(),
                id: "00000000-0000-0000-0000-000000000001".into(),
                longitude: -122.3,
                latitude: 47.6,
                source_object_index: 0,
                source_row_group: 0,
                source_row_index: 0,
                country: "US".into(),
                postal_city: String::new(),
                postcode: "98109".into(),
                street: "Broad Street".into(),
                number: "400".into(),
                unit: String::new(),
                address_levels: vec!["WA".into(), "SEATTLE".into()],
            }],
        };
        assert_eq!(
            address_lookup_disposition(&matched, "build-1").unwrap(),
            AddressLookupDisposition::Match
        );

        let out_of_coverage = AddressOutcome::OutOfCoverage {
            data_version: "build-1".into(),
            normalization_version: "normalization-1",
        };
        assert_eq!(
            address_lookup_disposition(&out_of_coverage, "build-1").unwrap(),
            AddressLookupDisposition::OutOfCoverage
        );
        assert!(address_lookup_disposition(&empty, "other-build").is_err());
    }

    #[test]
    fn proximity_uses_standard_longitude_latitude_order() {
        let params = HashMap::from([("proximity".into(), "-71.1,42.48".into())]);
        assert_eq!(parse_proximity(&params).unwrap(), Some((-71.1, 42.48)));
    }

    fn division_result(
        id: &str,
        name: &str,
        division_type: &str,
        longitude: f64,
        latitude: f64,
    ) -> geocoder_core::GeocoderResult {
        geocoder_core::GeocoderResult {
            gers_id: id.into(),
            primary_name: name.into(),
            lat: latitude,
            lon: longitude,
            bbox: [longitude, latitude, longitude, latitude],
            importance: 1.0,
            division_type: division_type.into(),
            country: None,
            region: None,
            population: None,
            search_name: None,
        }
    }

    fn division_result_with_alternates(
        id: &str,
        name: &str,
        division_type: &str,
        longitude: f64,
        latitude: f64,
        search_name: &str,
    ) -> geocoder_core::GeocoderResult {
        geocoder_core::GeocoderResult {
            search_name: Some(search_name.into()),
            ..division_result(id, name, division_type, longitude, latitude)
        }
    }

    fn head_place(name: &str) -> PlaceProjection {
        PlaceProjection {
            id: name.into(),
            latitude: 0.0,
            longitude: 0.0,
            confidence: 1.0,
            prominence: 0.0,
            name: name.into(),
            category: "monument".into(),
            locality: String::new(),
            region: String::new(),
            country: String::new(),
            distance_km: None,
        }
    }

    #[test]
    fn locality_suffix_fallback_accepts_bounded_long_name_locality_queries() {
        let tokens = query_terms("Museum of Modern Art New York");
        assert_eq!(tokens.len(), 6);
        assert_eq!(
            locality_suffix_candidates(&tokens, false, &[]),
            vec![
                LocalitySuffixCandidate {
                    place_query: "museum of modern art".into(),
                    locality_query: "new york".into(),
                    locality_tokens: vec!["new".into(), "york".into()],
                },
                LocalitySuffixCandidate {
                    place_query: "museum of modern art new".into(),
                    locality_query: "york".into(),
                    locality_tokens: vec!["york".into()],
                },
            ]
        );
        assert!(locality_suffix_candidates(
            &query_terms("The Museum of Modern Art New York"),
            false,
            &[],
        )
        .is_empty());

        let tokens = query_terms("Modern Art New York");
        let candidates = locality_suffix_candidates(&tokens, false, &[]);
        assert_eq!(
            candidates,
            vec![
                LocalitySuffixCandidate {
                    place_query: "modern art".into(),
                    locality_query: "new york".into(),
                    locality_tokens: vec!["new".into(), "york".into()],
                },
                LocalitySuffixCandidate {
                    place_query: "modern art new".into(),
                    locality_query: "york".into(),
                    locality_tokens: vec!["york".into()],
                },
            ]
        );
        assert!(locality_suffix_candidates(&tokens, true, &[]).is_empty());
        // Two tokens now DO produce a candidate. `Eiffel Tower` yields the
        // suffix `tower`, which matches no locality-typed division, so the
        // inference finds nothing and the result is unchanged -- while the same
        // widening is what lets `IKEA Berlin` resolve at all (RC6).
        assert_eq!(
            locality_suffix_candidates(&query_terms("Eiffel Tower"), false, &[]),
            vec![LocalitySuffixCandidate {
                place_query: "eiffel".into(),
                locality_query: "tower".into(),
                locality_tokens: vec!["tower".into()],
            }]
        );
        assert_eq!(
            locality_suffix_candidates(&query_terms("IKEA Berlin"), false, &[]),
            vec![LocalitySuffixCandidate {
                place_query: "ikea".into(),
                locality_query: "berlin".into(),
                locality_tokens: vec!["berlin".into()],
            }]
        );
        // A single token still cannot be split.
        assert!(locality_suffix_candidates(&query_terms("Berlin"), false, &[]).is_empty());
        // And the fallback still never runs when the head already answered.
        assert!(
            locality_suffix_candidates(&query_terms("IKEA Berlin"), false, &[head_place("IKEA")],)
                .is_empty(),
            "a non-empty head must suppress the fallback so it can never displace a real answer"
        );
    }

    #[test]
    fn three_token_head_keeps_exact_name_locality_routing_without_misreading_liberty() {
        assert_eq!(
            locality_suffix_candidates(
                &query_terms("Taj Mahal Agra"),
                false,
                &[head_place("Taj Mahal")],
            ),
            vec![LocalitySuffixCandidate {
                place_query: "taj mahal".into(),
                locality_query: "agra".into(),
                locality_tokens: vec!["agra".into()],
            }]
        );
        assert!(locality_suffix_candidates(
            &query_terms("Statue of Liberty"),
            false,
            &[head_place("Statue of Liberty National Monument")],
        )
        .is_empty());
        assert_eq!(
            locality_suffix_candidates(
                &query_terms("Union Station Toronto"),
                false,
                &[
                    head_place("Union Station Toronto"),
                    head_place("Union Station"),
                ],
            ),
            vec![LocalitySuffixCandidate {
                place_query: "union station".into(),
                locality_query: "toronto".into(),
                locality_tokens: vec!["toronto".into()],
            }]
        );
    }

    #[test]
    fn locality_suffix_match_is_exact_and_locality_like() {
        let candidate = LocalitySuffixCandidate {
            place_query: "museum modern art".into(),
            locality_query: "new york".into(),
            locality_tokens: vec!["new".into(), "york".into()],
        };
        let results = vec![
            division_result("region", "New York", "region", -75.0, 43.0),
            division_result("partial", "York", "locality", -1.08, 53.96),
            division_result("exact", "New York, NY", "locality", -74.006, 40.7128),
        ];
        assert_eq!(
            exact_locality_results(&candidate, &results)
                .iter()
                .map(|result| result.gers_id.as_str())
                .collect::<Vec<_>>(),
            vec!["exact"]
        );
    }

    fn locality_candidate(place: &str, locality: &str) -> LocalitySuffixCandidate {
        LocalitySuffixCandidate {
            place_query: place.into(),
            locality_query: locality.into(),
            locality_tokens: query_terms(locality),
        }
    }

    #[test]
    fn alternate_division_names_resolve_endonym_localities() {
        // Ciudad de Mexico / Tokyo: the query never equals the primary name,
        // so only the alternate-name rung can route these.
        let candidate = locality_candidate("hotel del angel", "mexico city");
        let results = vec![
            division_result("elsewhere", "Mexico", "region", -102.0, 23.0),
            division_result_with_alternates(
                "cdmx",
                "Ciudad de Mexico",
                "locality",
                -99.13,
                19.43,
                "Ciudad de Mexico;Mexico City;CDMX",
            ),
        ];
        assert_eq!(
            exact_locality_results(&candidate, &results)
                .iter()
                .map(|result| result.gers_id.as_str())
                .collect::<Vec<_>>(),
            vec!["cdmx"]
        );

        let tokyo = locality_candidate("apple marunouchi", "tokyo");
        let results = vec![division_result_with_alternates(
            "tokyo",
            "\u{6771}\u{4eac}\u{90fd}",
            "locality",
            139.69,
            35.69,
            "\u{6771}\u{4eac}\u{90fd};Tokyo;Tokyo Metropolis",
        )];
        assert_eq!(
            exact_locality_results(&tokyo, &results)
                .iter()
                .map(|result| result.gers_id.as_str())
                .collect::<Vec<_>>(),
            vec!["tokyo"]
        );
    }

    #[test]
    fn alternate_names_never_displace_an_exact_primary_name_match() {
        // RC3 guard shape: whatever the exact rung picks today stays the first
        // attempt, and a partial alternate name is not a match at all.
        let candidate = locality_candidate("taj mahal", "agra");
        let results = vec![
            division_result_with_alternates(
                "alt",
                "Agra Cantonment",
                "locality",
                78.0,
                27.15,
                "Agra Cantonment;Agra",
            ),
            division_result("primary", "Agra", "locality", 78.02, 27.18),
            division_result_with_alternates(
                "partial",
                "Fatehpur",
                "locality",
                77.66,
                27.09,
                "Fatehpur;Agra district",
            ),
        ];
        assert_eq!(
            exact_locality_results(&candidate, &results)
                .iter()
                .map(|result| result.gers_id.as_str())
                .collect::<Vec<_>>(),
            vec!["primary", "alt"]
        );
    }

    #[test]
    fn locality_matches_are_bounded_and_locality_like() {
        let candidate = locality_candidate("mayo clinic", "rochester");
        let mut results = vec![division_result(
            "region",
            "Rochester",
            "region",
            -77.6,
            43.2,
        )];
        for index in 0..6 {
            results.push(division_result(
                &format!("rochester-{index}"),
                "Rochester",
                "locality",
                -92.46,
                44.02,
            ));
        }
        let matches = exact_locality_results(&candidate, &results);
        assert_eq!(matches.len(), LOCALITY_INFERENCE_ATTEMPT_CAP);
        assert_eq!(
            matches
                .iter()
                .map(|result| result.gers_id.as_str())
                .collect::<Vec<_>>(),
            vec!["rochester-0", "rochester-1", "rochester-2"]
        );
    }

    #[test]
    fn homonym_retry_adopts_the_first_non_empty_routed_attempt() {
        // Attempt 0 empty: adopted (pre-retry behaviour) but does not stop.
        assert_eq!(locality_attempt_disposition(0, true), (true, false));
        // Attempt 1 non-empty: adopted, and stops the loop.
        assert_eq!(locality_attempt_disposition(1, false), (true, true));
        // A later empty attempt never overwrites the first adoption.
        assert_eq!(locality_attempt_disposition(1, true), (false, false));
        assert_eq!(locality_attempt_disposition(2, true), (false, false));
        // A query that works today: first attempt non-empty, adopted, stop.
        assert_eq!(locality_attempt_disposition(0, false), (true, true));
    }

    #[test]
    fn already_working_name_plus_locality_queries_keep_their_plan() {
        // "Taj Mahal Agra" style: the head contains the exact prefix name, one
        // candidate survives, and the routed plan is unchanged by the retry
        // work -- first plan, same centroid, same trimmed place query.
        let tokens = query_terms("Taj Mahal Agra");
        let candidates = locality_suffix_candidates(&tokens, false, &[head_place("Taj Mahal")]);
        assert_eq!(candidates.len(), 1);
        let candidate = &candidates[0];
        assert_eq!(candidate.place_query, "taj mahal");
        let divisions = vec![
            division_result("agra", "Agra", "locality", 78.02, 27.18),
            division_result_with_alternates(
                "agra-cantt",
                "Agra Cantonment",
                "locality",
                78.0,
                27.15,
                "Agra Cantonment;Agra",
            ),
        ];
        let plans = exact_locality_results(candidate, &divisions)
            .into_iter()
            .map(|result| places_locality_inference(candidate, result))
            .collect::<Vec<_>>();
        assert_eq!(plans.len(), 2);
        assert_eq!(plans[0].0, "taj mahal");
        assert_eq!(plans[0].1.division_id, "agra");
        assert_eq!((plans[0].1.longitude, plans[0].1.latitude), (78.02, 27.18));
        // And the first attempt's disposition stops the loop as soon as that
        // routed search returns anything, so rank 1 cannot move.
        assert_eq!(locality_attempt_disposition(0, false), (true, true));
    }

    #[test]
    fn locality_suffix_plan_routes_remaining_name_at_the_centroid() {
        let candidate = locality_suffix_candidates(&query_terms("Matisse Museum Nice"), false, &[])
            .pop()
            .unwrap();
        let division = division_result("nice", "Nice", "locality", 7.262, 43.71);
        let (place_query, inference) = places_locality_inference(&candidate, &division);
        assert_eq!(place_query, "matisse museum");
        assert_eq!(inference.locality_query, "nice");
        assert_eq!((inference.longitude, inference.latitude), (7.262, 43.71));
    }

    #[test]
    fn inferred_centroid_distance_is_cleared_and_metadata_is_explicit() {
        let inference = PlacesLocalityInference {
            locality_query: "nice".into(),
            division_id: "nice".into(),
            division_type: "locality".into(),
            longitude: 7.262,
            latitude: 43.71,
        };
        let mut places = vec![PlaceProjection {
            id: "museum".into(),
            latitude: 43.71,
            longitude: 7.276,
            confidence: 0.9,
            prominence: 0.0,
            name: "Matisse Museum".into(),
            category: "museum".into(),
            locality: "Nice".into(),
            region: "Provence-Alpes-Cote d'Azur".into(),
            country: "FR".into(),
            distance_km: Some(1.1),
        }];
        let marker = apply_places_locality_inference(&mut places, &inference);
        assert_eq!(places[0].distance_km, None);
        assert_eq!(marker["query"], "nice");
        assert_eq!(marker["division_id"], "nice");
        assert_eq!(marker["routing"], "locality_centroid");

        let types = HashSet::from(["poi".into()]);
        let metadata = text_metadata(&types, None, Some(marker), None);
        assert_eq!(
            metadata["places_locality_inference"]["division_type"],
            "locality"
        );
        assert!(metadata["proximity"].is_null());
        let plain = text_metadata(&types, None, None, None);
        assert!(plain.get("places_locality_inference").is_none());
        // A response the fallback did not touch carries no fallback marker.
        assert!(plain.get("places_prefix_head_fallback").is_none());
    }

    #[test]
    fn prefix_head_fallback_marker_names_the_probe_and_the_verified_tail() {
        let tokens = query_terms("Geylang Bahru MRT Station");
        let marker = prefix_head_fallback_metadata(&tokens).expect("four tokens are in range");
        assert_eq!(marker["probe_query"], "geylang bahru mrt");
        assert_eq!(marker["verified_tokens"], json!(["station"]));
        assert_eq!(marker["verification"], "display_fields");

        let types = HashSet::from(["poi".into()]);
        let metadata = text_metadata(&types, None, None, Some(marker));
        assert_eq!(
            metadata["places_prefix_head_fallback"]["probe_query"],
            "geylang bahru mrt"
        );
        assert!(metadata.get("places_locality_inference").is_none());

        // Widths the ordinary head lane already serves never produce a marker,
        // which is the same predicate that keeps the fallback itself inert
        // there.
        assert!(prefix_head_fallback_metadata(&query_terms("Yishun MRT Station")).is_none());
        assert!(
            prefix_head_fallback_metadata(&query_terms("one two three four five six seven"))
                .is_none()
        );
    }

    #[test]
    fn id_lookup_accepts_only_uuid_shaped_gers_ids() {
        assert!(valid_gers_id("08b2a100d6644b64b2f70e9f6e46886f"));
        assert!(valid_gers_id("08b2a100-d664-4b64-b2f7-0e9f6e46886f"));
        assert!(!valid_gers_id("zzb2a100-d664-4b64-b2f7-0e9f6e46886f"));
        assert!(!valid_gers_id("08b2a100d664-4b64-b2f7-0e9f6e46886f"));
    }

    #[test]
    fn v2_id_response_matches_legacy_shape_and_adds_atomic_version() {
        let body = id_response_body(
            &IdLookupResult {
                id: "08b2a100-d664-4b64-b2f7-0e9f6e46886f".into(),
                bbox: geocoder_core::BBox {
                    xmin: -122.4,
                    ymin: 47.5,
                    xmax: -122.3,
                    ymax: 47.7,
                },
                locator: None,
            },
            &DataVersion {
                overture_release: "2026-06-17.0".into(),
                geocoder_build: "2026-07-29.0".into(),
            },
        );
        assert_eq!(body["id"], "08b2a100-d664-4b64-b2f7-0e9f6e46886f");
        assert_eq!(body["bbox"]["xmin"], -122.4);
        assert_eq!(body["data_version"]["overture_release"], "2026-06-17.0");
        assert_eq!(body["data_version"]["geocoder_build"], "2026-07-29.0");
        assert!(body.get("type").is_none());
        assert!(body.get("geometry").is_none());
        assert!(body.get("properties").is_none());
    }
}
