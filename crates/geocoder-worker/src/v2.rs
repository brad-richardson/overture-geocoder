//! Unified v2 geocoding API and atomic release discovery.

use std::collections::{HashMap, HashSet};

use geocoder_core::{GeocoderQuery, LocationBias};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use worker::*;

use crate::address::{build_lookup_key, AddressOutcome};
use crate::places_pages::{query_terms, PlaceProjection, PlacesClause, TOKENIZER_VERSION};
use crate::stac::cache::{CATALOG_CACHE_TTL, IMMUTABLE_CACHE_TTL};
use crate::stac::{ShardLoader, UserLocation, NOT_FOUND_SENTINEL};

const CATALOG_SCHEMA: &str = "overture-geocoder-v2-catalog-v1";
const RELEASE_SCHEMA: &str = "overture-geocoder-v2-release-v1";
const PLACES_FORMAT_VERSION: &str = "PCSH0001";
const ADDRESS_FORMAT_VERSION: &str = "address-reduce-2";
const ADDRESS_NORMALIZATION_VERSION: &str = "nfc-uniws-collapse-ascii-lower-1";
const MAX_CATALOG_RELEASES: usize = 64;
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
    bytes: usize,
    sha256: String,
}

#[derive(Debug, Deserialize)]
struct FamilySource {
    kind: String,
    version: String,
    manifest_key: String,
    manifest_sha256: String,
}

#[derive(Debug, Deserialize)]
struct FamilyVersions {
    format: String,
    tokenizer: Option<String>,
    normalization: Option<String>,
}

#[derive(Debug, Deserialize)]
struct FamilyReference {
    source: FamilySource,
    manifest_key: String,
    manifest_digest: String,
    manifest_sha256: String,
    versions: FamilyVersions,
    operations: Vec<String>,
    entrypoints: HashMap<String, ArtifactIdentity>,
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

    fn family_entrypoint(&self, family: &str, operation: &str) -> Option<&ArtifactIdentity> {
        self.families.get(family)?.entrypoints.get(operation)
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

fn validate_catalog(catalog: &V2Catalog) -> std::result::Result<&CatalogEntry, String> {
    if catalog.schema != CATALOG_SCHEMA
        || !valid_build(&catalog.latest)
        || !valid_sha256(&catalog.catalog_digest)
        || catalog.releases.is_empty()
        || catalog.releases.len() > MAX_CATALOG_RELEASES
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
            || entry.manifest_key != format!("v2/releases/{}/release.json", entry.geocoder_build)
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

fn validate_family(name: &str, family: &FamilyReference) -> std::result::Result<(), String> {
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
    if (name == "places"
        && (family.versions.format != PLACES_FORMAT_VERSION
            || family.operations != ["forward"]
            || family.versions.tokenizer.as_deref() != Some(TOKENIZER_VERSION)
            || family.versions.normalization.is_some()))
        || (name == "addresses"
            && (family.versions.format != ADDRESS_FORMAT_VERSION
                || family.operations != ["structured_forward"]
                || family.versions.normalization.as_deref() != Some(ADDRESS_NORMALIZATION_VERSION)
                || family.versions.tokenizer.is_some()))
    {
        return Err(format!("v2 {name} family versions are unsupported"));
    }
    for (operation, identity) in &family.entrypoints {
        let prefix = format!("{}/families/{name}/", family.source.version);
        if !identity.object_key.starts_with(&prefix)
            || !safe_key(&identity.object_key)
            || identity.bytes == 0
            || !valid_sha256(&identity.sha256)
            || (name == "places"
                && identity.object_key
                    != format!("{}/families/places/catalog.pcat", family.source.version))
            || (name == "addresses"
                && identity.object_key
                    != format!(
                        "{}/families/addresses/address-collection.json",
                        family.source.version
                    ))
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
        validate_family(name, family)?;
        for operation in &family.operations {
            if !release.supports(operation, name) {
                return Err(format!("v2 {name} capability is absent at top level"));
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

impl ShardLoader {
    pub(crate) async fn load_v2_release(&self) -> Result<V2Release> {
        let catalog_key = "v2/catalog.json";
        let catalog_text = self
            .memoized_get_text(catalog_key, CATALOG_CACHE_TTL)
            .await?
            .ok_or_else(|| crate::stac::not_found(catalog_key))?;
        let catalog: V2Catalog = serde_json::from_str(&catalog_text)
            .map_err(|error| Error::RustError(format!("Invalid {catalog_key}: {error}")))?;
        let entry = validate_catalog(&catalog).map_err(Error::RustError)?;
        let manifest_text = self
            .memoized_get_text(&entry.manifest_key, IMMUTABLE_CACHE_TTL)
            .await?
            .ok_or_else(|| crate::stac::not_found(&entry.manifest_key))?;
        let actual_sha = format!("{:x}", Sha256::digest(manifest_text.as_bytes()));
        if actual_sha != entry.manifest_sha256 {
            return Err(Error::RustError(
                "v2 release manifest SHA-256 differs from catalog".into(),
            ));
        }
        let release: V2Release = serde_json::from_str(&manifest_text).map_err(|error| {
            Error::RustError(format!("Invalid {}: {error}", entry.manifest_key))
        })?;
        validate_release(&release, entry).map_err(Error::RustError)?;
        Ok(release)
    }
}

enum ReleaseAvailability {
    Ready(Box<V2Release>),
    Unavailable(Response),
}

async fn load_available_release(loader: &ShardLoader) -> Result<ReleaseAvailability> {
    match loader.load_v2_release().await {
        Ok(release) => Ok(ReleaseAvailability::Ready(Box::new(release))),
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

fn place_feature(place: &PlaceProjection) -> (f64, Value) {
    let score = f64::from(place.confidence).clamp(0.0, 1.0);
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

async fn search_places(
    loader: &ShardLoader,
    entrypoint: &ArtifactIdentity,
    query: &str,
    proximity: Option<(f64, f64)>,
    autocomplete: bool,
    limit: usize,
) -> Result<Vec<PlaceProjection>> {
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
        let Some(route) = catalog.catalog.route_point(longitude, latitude).cloned() else {
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
    if tokens.len() > 2 {
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
        let key = match build_lookup_key(&address_params(&params)) {
            Ok(value) => value,
            Err(error) => return json_error("invalid_request", &error.message(), 400),
        };
        let Some(entrypoint) = release.family_entrypoint("addresses", "structured_forward") else {
            return json_error(
                "capability_unavailable",
                "structured address data is unavailable",
                503,
            );
        };
        let outcome = loader
            .lookup_address_entrypoint(&key, &entrypoint.object_key, &release.geocoder_build)
            .await?;
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
    let mut ranked = Vec::new();
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
        ranked.extend(search.results.iter().map(division_feature));
    }
    if types.contains("poi") {
        let Some(entrypoint) = release.family_entrypoint("places", "forward") else {
            if params.contains_key("types") {
                return json_error("capability_unavailable", "Places data is unavailable", 503);
            }
            let features = ranked.into_iter().map(|(_, feature)| feature).collect();
            let body = data_version_body(
                &release.data_version,
                features,
                json!({"mode": "text", "places": "unavailable"}),
            );
            return versioned_response(&body, &release.data_version, 200);
        };
        let places = search_places(
            &loader,
            entrypoint,
            query.as_str(),
            proximity,
            autocomplete,
            limit,
        )
        .await?;
        ranked.extend(places.iter().map(place_feature));
    }
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
        json!({"mode": "text", "types": types, "proximity": proximity}),
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
    if types.contains("poi") || types.contains("address") {
        return json_error(
            "capability_unavailable",
            "v2 reverse currently supports division types only",
            400,
        );
    }
    let loader = ShardLoader::with_context(&ctx.env, ctx.data.clone())?;
    let release = match load_available_release(&loader).await? {
        ReleaseAvailability::Ready(release) => release,
        ReleaseAvailability::Unavailable(response) => return Ok(response),
    };
    let search = loader
        .reverse_geocode_version(release.core_version(), latitude, longitude)
        .await?;
    let features = search
        .result
        .filter(|result| types.contains(&normalized_type(&result.subtype)))
        .map_or_else(Vec::new, |result| {
            vec![json!({
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
            })]
        });
    let body = data_version_body(
        &release.data_version,
        features,
        json!({"mode": "reverse", "query": {"longitude": longitude, "latitude": latitude}}),
    );
    versioned_response(&body, &release.data_version, 200)
}

pub(crate) async fn handle_feature(
    _req: Request,
    ctx: RouteContext<std::rc::Rc<Context>>,
) -> Result<Response> {
    let identity = ctx
        .param("gers_id")
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
    let feature = json!({
        "type": "Feature",
        "id": result.id,
        "geometry": Value::Null,
        "bbox": result.bbox,
        "properties": result,
        "data_version": release.data_version.clone(),
    });
    versioned_response(&feature, &release.data_version, 200)
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

    fn release() -> V2Release {
        serde_json::from_value(json!({
            "schema": RELEASE_SCHEMA,
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
        }))
        .unwrap()
    }

    #[test]
    fn catalog_validation_pins_latest_and_canonical_manifest_path() {
        assert_eq!(
            validate_catalog(&catalog()).unwrap().geocoder_build,
            "2026-07-19.1"
        );
        let mut bad = catalog();
        bad.releases[0].manifest_key = "../release.json".into();
        assert!(validate_catalog(&bad).is_err());
    }

    #[test]
    fn release_validation_accepts_only_capabilities_the_worker_can_serve() {
        validate_release(&release(), &catalog().releases[0]).unwrap();

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
    }

    #[test]
    fn type_parser_accepts_public_poi_aliases_and_rejects_unknowns() {
        let raw = "place,neighbourhood".to_string();
        let parsed = parse_types(Some(&raw)).unwrap();
        assert_eq!(parsed, HashSet::from(["poi".into(), "neighborhood".into()]));
        assert!(parse_types(Some(&"planet".to_string())).is_err());
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
    fn proximity_uses_standard_longitude_latitude_order() {
        let params = HashMap::from([("proximity".into(), "-71.1,42.48".into())]);
        assert_eq!(parse_proximity(&params).unwrap(), Some((-71.1, 42.48)));
    }

    #[test]
    fn feature_lookup_accepts_only_uuid_shaped_gers_ids() {
        assert!(valid_gers_id("08b2a100d6644b64b2f70e9f6e46886f"));
        assert!(valid_gers_id("08b2a100-d664-4b64-b2f7-0e9f6e46886f"));
        assert!(!valid_gers_id("zzb2a100-d664-4b64-b2f7-0e9f6e46886f"));
        assert!(!valid_gers_id("08b2a100d664-4b64-b2f7-0e9f6e46886f"));
    }
}
