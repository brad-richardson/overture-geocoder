#!/usr/bin/env python3
"""Build, validate, and publish the v2 release/catalog control plane.

The document layer (``release`` / ``validate-release`` / ``catalog`` /
``validate-catalog``) creates and proves the deterministic v2 release manifest
binding one complete legacy core release (division forward/reverse plus the ID
index) to optional Places and address family manifests from that same Overture
release, plus a ``v2/catalog.json`` candidate.  Those four subcommands never
access R2.

The publication layer makes ``/v2/forward`` live:

``assemble`` reads a promoted ``slice-YYYY-MM-DD.N`` construction tree plus the
frozen legacy core from R2 (or a ``local:`` mirror), hashes every byte the
worker will pin (family manifests, the slice source manifest, the routing
entrypoints, the legacy release manifest), and emits a canonical
``release.json`` locally.  ``publish-release`` is the create-only upload of
``v2/releases/{build}/release.json``; byte-identical re-runs succeed, different
bytes fail closed, and the catalog is never touched.  ``promote`` is a
compare-and-swap write of ``v2/catalog.json`` against an explicitly stated
expectation (``--expect-absent`` or ``--expect-sha256``), re-proving every
release reference against its source bytes first.  ``recover`` repoints the
catalog at a prior release under the same discipline, or ``--unavailable``
compare-and-swaps it to a signed unavailable document so the worker serves 503
``release_unavailable``.  Release documents are never deleted by tooling.  All
publication writes are dry-run by default (``--execute`` writes), are announced
with target key + SHA-256 before any write, and every written object is
re-downloaded and hashed afterwards.

CAS honesty: on R2 the swap sends ``If-Match`` with the ETag captured by the
SAME GET that hashed the current catalog, so the conditional PUT is atomic at
the store IF R2 enforces ``If-Match`` on PutObject.  That header is exercised
nowhere else in this repo; probe it with one object (PUT, then a second PUT
carrying a stale ``If-Match``, expecting 412) before the first production
promote -- the PR #181 discipline promote_construction_slice applies to
CopyObject.  The create-only leg (``If-None-Match: *``) is already
execution-proven live (PR #181).  On the ``local:`` backend the swap is
check-then-act with a small unavoidable race window between reading the current
bytes and replacing them; that backend exists for rehearsal, not production.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import global_build_manifest as gbm  # noqa: E402
from common import sha256_file, version_sort_key  # noqa: E402


RELEASE_SCHEMA = "overture-geocoder-v2-release-v1"
CATALOG_SCHEMA = "overture-geocoder-v2-catalog-v1"
UNAVAILABLE_CATALOG_SCHEMA = "overture-geocoder-v2-unavailable-v1"
UNAVAILABLE_REASON = "operator-recovery"
BUILD_RE = re.compile(r"\d{4}-\d{2}-\d{2}\.\d+")
SLICE_RE = re.compile(r"slice-\d{4}-\d{2}-\d{2}\.\d+")
KEY_COMPONENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
PRODUCTION_CATALOG_KEY = "v2/catalog.json"
PREVIEW_CATALOG_RE = re.compile(
    r"smoketest-v2/[A-Za-z0-9_-]{1,128}/catalog\.json"
)
FAMILY_OPERATIONS = {
    "addresses": {"forward", "reverse", "structured_forward"},
    "places": {"forward", "reverse"},
}
DEFAULT_FAMILY_OPERATIONS = {
    "addresses": ["structured_forward"],
    "places": ["forward"],
}
# Operation dependencies are keyed by (family, operation, versions.format):
# the worker's admission requires the canonical global head for the legacy
# PCSH0001 Places layout, and the routing.json entrypoint for the promoted
# construction formats (crates/geocoder-worker/src/v2.rs
# verify_v2_release_readiness / places_construction_admission /
# address_construction_admission).
FAMILY_OPERATION_DEPENDENCIES = {
    ("places", "forward", "PCSH0001"): ["families/places/head.phrp"],
    ("places", "forward", "PLRV0002+PLHD0002"): ["families/places/routing.json"],
    ("addresses", "structured_forward", "OAV1ART"): [
        "families/addresses/routing.json"
    ],
}
CORE_OPERATIONS = {
    "feature_lookup": ["id"],
    "forward": ["divisions"],
    "reverse": ["divisions"],
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_exact_fields(value: dict[str, Any], expected: set[str], kind: str) -> None:
    if set(value) != expected:
        raise ValueError(
            f"{kind} fields differ: missing={sorted(expected - set(value))}, "
            f"unknown={sorted(set(value) - expected)}"
        )


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} is required")
    return value


def _require_sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _require_build(value: Any) -> str:
    if not isinstance(value, str) or not BUILD_RE.fullmatch(value):
        raise ValueError("geocoder_build must use YYYY-MM-DD.N")
    return value


def _require_safe_key(value: Any, field: str) -> str:
    key = _require_string(value, field)
    if key.startswith("/") or any(part in ("", ".", "..") for part in key.split("/")):
        raise ValueError(f"{field} must be a canonical relative object key")
    return key


def _require_key_component(value: Any, field: str) -> str:
    component = _require_string(value, field)
    if not KEY_COMPONENT_RE.fullmatch(component):
        raise ValueError(f"{field} must be one canonical object-key component")
    return component


def _require_family_artifact_key(value: Any, family: str, field: str) -> str:
    key = _require_safe_key(value, field)
    if not key.startswith(f"families/{family}/"):
        raise ValueError(f"{field} must remain under families/{family}/")
    return key


def _require_published_family_artifact_key(
    value: Any, source_version: str, family: str, field: str
) -> str:
    key = _require_safe_key(value, field)
    prefix = f"{source_version}/families/{family}/"
    if not key.startswith(prefix):
        raise ValueError(f"{field} must remain under {prefix}")
    return key


def _validate_core_object(value: Any, label: str) -> tuple[str, int]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    href = _require_string(value.get("href"), f"{label} href")
    if not href.startswith("./"):
        raise ValueError(f"{label} href must be release-relative")
    _require_safe_key(href[2:], f"{label} href")
    size = gbm.require_int(value.get("size_bytes"), f"{label} bytes", minimum=1)
    _require_sha256(value.get("sha256"), f"{label} SHA-256")
    return href, size


def _validate_core_family_objects(
    value: Any,
    *,
    label: str,
    prefix: str,
    suffix: str,
) -> dict[str, int]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"legacy {label} objects must be a non-empty array")
    objects: dict[str, int] = {}
    for index, item in enumerate(value):
        href, size = _validate_core_object(item, f"legacy {label} object {index}")
        key = href[2:]
        if (
            not key.startswith(prefix)
            or not key.endswith(suffix)
            or key.count("/") != 1
        ):
            raise ValueError(f"legacy {label} object is outside {prefix}*{suffix}")
        if href in objects:
            raise ValueError(f"legacy {label} objects contain duplicate hrefs")
        objects[href] = size
    return objects


def _validate_legacy_release(manifest: Any, overture_release: str) -> dict[str, Any]:
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("legacy release manifest must use schema_version 1")
    version = _require_build(manifest.get("version"))
    if manifest.get("overture_release") != overture_release:
        raise ValueError("legacy release Overture release differs")
    _require_string(manifest.get("generated_at"), "legacy release generated_at")

    families = manifest.get("families")
    if not isinstance(families, dict) or set(families) != {
        "forward",
        "reverse",
        "id",
    }:
        raise ValueError("legacy release must contain exactly forward, reverse, and id")
    forward = families["forward"]
    reverse = families["reverse"]
    identifier = families["id"]
    if not all(isinstance(value, dict) for value in (forward, reverse, identifier)):
        raise ValueError("legacy core family summaries must be objects")
    if forward.get("collection") != "./collection.json":
        raise ValueError("legacy forward collection path differs")
    if reverse.get("collection") != "./reverse-collection.json":
        raise ValueError("legacy reverse collection path differs")
    if identifier.get("collection") != "./id-collection.json":
        raise ValueError("legacy ID collection path differs")

    forward_objects = _validate_core_family_objects(
        forward.get("objects"), label="forward", prefix="shards/", suffix=".db"
    )
    reverse_objects = _validate_core_family_objects(
        reverse.get("objects"), label="reverse", prefix="reverse/", suffix=".db"
    )
    id_objects = _validate_core_family_objects(
        identifier.get("objects"),
        label="ID",
        prefix="id-index/",
        suffix=".parquet",
    )
    if forward.get("shard_count") != len(forward_objects):
        raise ValueError("legacy forward shard count differs")
    if reverse.get("shard_count") != len(reverse_objects):
        raise ValueError("legacy reverse shard count differs")
    expected_id_hrefs = {
        f"./id-index/{prefix:03x}.parquet" for prefix in range(16**3)
    }
    if (
        identifier.get("format_version") != 3
        or identifier.get("shard_count") != 4096
        or set(id_objects) != expected_id_hrefs
    ):
        raise ValueError("legacy ID family must contain the exact v3 4096-shard set")
    if identifier.get("total_size_bytes") != sum(id_objects.values()):
        raise ValueError("legacy ID total bytes differ")

    router_href, router_size = _validate_core_object(
        forward.get("router"), "legacy forward router"
    )
    if router_href != "./router.db":
        raise ValueError("legacy forward router path differs")
    dictionary_href, dictionary_size = _validate_core_object(
        identifier.get("locator_dictionary"), "legacy ID locator dictionary"
    )
    dictionary_sha256 = identifier["locator_dictionary"]["sha256"]
    if dictionary_href != f"./id-locator-dictionary-{dictionary_sha256}.json":
        raise ValueError("legacy ID locator dictionary path differs from its SHA-256")
    existing_core_hrefs = (
        set(forward_objects)
        | set(reverse_objects)
        | set(id_objects)
        | {router_href}
    )
    if dictionary_href in existing_core_hrefs:
        raise ValueError("legacy ID locator dictionary href aliases a core object")

    verified = manifest.get("verified_version_objects")
    if not isinstance(verified, list) or not verified:
        raise ValueError("legacy release has no verified object set")
    verified_sizes: dict[str, int] = {}
    for index, item in enumerate(verified):
        if not isinstance(item, dict):
            raise ValueError(f"legacy verified object {index} is invalid")
        href = _require_string(item.get("href"), f"legacy verified object {index} href")
        if not href.startswith("./"):
            raise ValueError("legacy verified object href must be release-relative")
        _require_safe_key(href[2:], f"legacy verified object {index} href")
        size = gbm.require_int(
            item.get("size_bytes"), f"legacy verified object {index} bytes", minimum=1
        )
        if href in verified_sizes:
            raise ValueError("legacy verified object set contains duplicate hrefs")
        verified_sizes[href] = size

    bound_objects = {
        **forward_objects,
        **reverse_objects,
        **id_objects,
        router_href: router_size,
        dictionary_href: dictionary_size,
    }
    for href, size in bound_objects.items():
        if verified_sizes.get(href) != size:
            raise ValueError(f"legacy verified object set differs for {href}")
    required_metadata = {
        "./collection.json",
        "./reverse-collection.json",
        "./id-collection.json",
    }
    if not required_metadata.issubset(verified_sizes):
        raise ValueError("legacy verified object set omits a core collection")
    return {"version": version}


def _validate_family_source(
    family: str,
    source_manifest: Any,
    source_manifest_sha256: str,
    family_manifest: dict[str, Any],
    overture_release: str,
) -> dict[str, Any]:
    """Bind a family to a finalizer-produced release or family-only slice."""
    if (
        not isinstance(source_manifest, dict)
        or source_manifest.get("schema_version") != 1
    ):
        raise ValueError(f"{family} source manifest must use schema_version 1")
    if source_manifest.get("overture_release") != overture_release:
        raise ValueError(f"{family} source manifest Overture release differs")

    if source_manifest.get("is_slice") is True:
        if source_manifest.get("promotion_eligible") is not False:
            raise ValueError(f"{family} slice source must be non-promoting")
        version = _require_key_component(
            source_manifest.get("slice_version"), f"{family} slice version"
        )
        if not SLICE_RE.fullmatch(version):
            raise ValueError(f"{family} slice version must use slice-YYYY-MM-DD.N")
        summaries = source_manifest.get("families")
        source_kind = "family_slice"
        source_key = f"{version}/slice-manifest.json"
    else:
        version = _require_build(source_manifest.get("version"))
        summaries = source_manifest.get("optional_families")
        source_kind = "core_release"
        source_key = f"{version}/release-manifest.json"

    if not isinstance(summaries, dict) or not isinstance(summaries.get(family), dict):
        raise ValueError(f"{family} is not verified by its source manifest")
    summary = summaries[family]
    expected_manifest = f"./families/{family}/family-manifest.json"
    if summary.get("manifest") != expected_manifest:
        raise ValueError(f"{family} source manifest path differs")
    if summary.get("manifest_digest") != family_manifest["manifest_digest"]:
        raise ValueError(f"{family} source manifest digest differs")
    if summary.get("region") != family_manifest["region"]:
        raise ValueError(f"{family} source coverage differs")
    artifacts = family_manifest["artifacts"]
    if summary.get("artifact_count") != len(artifacts) or summary.get(
        "total_bytes"
    ) != sum(artifact["bytes"] for artifact in artifacts):
        raise ValueError(f"{family} source artifact totals differ")
    if summary.get("promotion_eligible") is not False:
        raise ValueError(f"{family} source summary must be non-promoting")

    objects = summary.get("objects")
    if not isinstance(objects, list):
        raise ValueError(f"{family} source objects must be an array")
    objects_by_href: dict[str, dict[str, Any]] = {}
    for value in objects:
        if not isinstance(value, dict) or not isinstance(value.get("href"), str):
            raise ValueError(f"{family} source object is invalid")
        if value["href"] in objects_by_href:
            raise ValueError(f"{family} source objects contain duplicate hrefs")
        objects_by_href[value["href"]] = value
    expected_hrefs = {f"./{artifact['object_key']}" for artifact in artifacts}
    if set(objects_by_href) != expected_hrefs:
        raise ValueError(f"{family} source artifact set differs")
    for artifact in artifacts:
        value = objects_by_href[f"./{artifact['object_key']}"]
        if value.get("size_bytes") != artifact["bytes"] or value.get(
            "sha256"
        ) != artifact["sha256"]:
            raise ValueError(f"{family} source artifact identity differs")

    verified_objects = source_manifest.get("verified_version_objects")
    if not isinstance(verified_objects, list):
        raise ValueError(f"{family} source has no verified object set")
    verified_hrefs = {
        value.get("href") for value in verified_objects if isinstance(value, dict)
    }
    if not ({expected_manifest} | expected_hrefs).issubset(verified_hrefs):
        raise ValueError(f"{family} source verified object set is incomplete")

    return {
        "kind": source_kind,
        "version": version,
        "manifest_key": source_key,
        "manifest_sha256": _require_sha256(
            source_manifest_sha256, f"{family} source manifest SHA-256"
        ),
    }


def _normalize_family_operations(
    family: str, requested: Any | None
) -> list[str]:
    if requested is None:
        requested = DEFAULT_FAMILY_OPERATIONS[family]
    if not isinstance(requested, list) or not requested:
        raise ValueError(f"{family} operations must be a non-empty JSON array")
    if any(not isinstance(value, str) or not value for value in requested):
        raise ValueError(f"{family} operations must contain non-empty strings")
    normalized = sorted(set(requested))
    unsupported = set(normalized) - FAMILY_OPERATIONS[family]
    if unsupported:
        raise ValueError(f"unsupported {family} operations: {sorted(unsupported)}")
    return normalized


def _derive_operations(families: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    operations = {name: values.copy() for name, values in CORE_OPERATIONS.items()}
    for family, reference in sorted(families.items()):
        for operation in reference["operations"]:
            operations.setdefault(operation, []).append(family)
    return {name: sorted(values) for name, values in sorted(operations.items())}


def _require_consistent_family_source_keys(
    families: dict[str, dict[str, Any]],
) -> None:
    identities: dict[str, str] = {}
    for family, reference in sorted(families.items()):
        source = reference["source"]
        key = source["manifest_key"]
        sha256 = source["manifest_sha256"]
        previous = identities.setdefault(key, sha256)
        if previous != sha256:
            raise ValueError(
                f"family source manifest key has conflicting SHA-256 values: {key}"
            )


def build_release_manifest(
    *,
    geocoder_build: str,
    overture_release: str,
    legacy_release: Any,
    legacy_manifest_sha256: str,
    family_manifests: dict[str, tuple[Any, str]] | None = None,
    family_source_manifests: dict[str, tuple[Any, str]] | None = None,
    family_operations: dict[str, list[str]] | None = None,
    family_entrypoints: dict[str, dict[str, str]] | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Bind one legacy core and zero or more optional family manifests."""
    geocoder_build = _require_build(geocoder_build)
    overture_release = _require_string(overture_release, "overture_release")
    generated_at = _require_string(generated_at or _now(), "generated_at")
    legacy = _validate_legacy_release(legacy_release, overture_release)
    legacy_sha = _require_sha256(legacy_manifest_sha256, "legacy manifest SHA-256")

    supplied_operations = family_operations or {}
    supplied_entrypoints = family_entrypoints or {}
    supplied_sources = family_source_manifests or {}
    if set(supplied_sources) != set(family_manifests or {}):
        raise ValueError("every family must have exactly one verified source manifest")
    unknown_operation_families = set(supplied_operations) - set(family_manifests or {})
    if unknown_operation_families:
        raise ValueError(
            "operations supplied for absent families: "
            f"{sorted(unknown_operation_families)}"
        )
    unknown_entrypoint_families = set(supplied_entrypoints) - set(family_manifests or {})
    if unknown_entrypoint_families:
        raise ValueError(
            "entrypoints supplied for absent families: "
            f"{sorted(unknown_entrypoint_families)}"
        )

    references: dict[str, dict[str, Any]] = {}
    for family, source in sorted((family_manifests or {}).items()):
        if family not in gbm.FAMILIES:
            raise ValueError(f"unsupported v2 family: {family}")
        if not isinstance(source, tuple) or len(source) != 2:
            raise ValueError(f"{family} family input must be (manifest, sha256)")
        manifest, manifest_sha = source
        validated = gbm.validate_family_manifest(manifest)
        if validated["family"] != family:
            raise ValueError(f"{family} family manifest declares another family")
        if validated["lineage"]["overture_release"] != overture_release:
            raise ValueError(f"{family} family Overture release differs")
        source_input = supplied_sources[family]
        if not isinstance(source_input, tuple) or len(source_input) != 2:
            raise ValueError(f"{family} source input must be (manifest, sha256)")
        source_manifest, source_sha = source_input
        source = _validate_family_source(
            family,
            source_manifest,
            source_sha,
            validated,
            overture_release,
        )
        operations = _normalize_family_operations(
            family, supplied_operations.get(family)
        )
        entrypoints = supplied_entrypoints.get(family)
        if not isinstance(entrypoints, dict) or set(entrypoints) != set(operations):
            raise ValueError(
                f"{family} entrypoints must name exactly its advertised operations"
            )
        artifacts_by_key = {
            artifact["object_key"]: artifact for artifact in validated["artifacts"]
        }
        for operation in operations:
            for required_key in FAMILY_OPERATION_DEPENDENCIES.get(
                (family, operation, validated["versions"]["format"]), []
            ):
                if required_key not in artifacts_by_key:
                    raise ValueError(
                        f"{family} {operation} requires manifest artifact "
                        f"{required_key}"
                    )
        normalized_entrypoints: dict[str, dict[str, Any]] = {}
        for operation, key in sorted(entrypoints.items()):
            safe_key = _require_family_artifact_key(
                key, family, f"{family} {operation} entrypoint"
            )
            artifact = artifacts_by_key.get(safe_key)
            if artifact is None:
                raise ValueError(
                    f"{family} {operation} entrypoint is not a manifest artifact"
                )
            normalized_entrypoints[operation] = {
                # Family manifests use keys relative to the immutable release
                # prefix. V2 entrypoints are bucket-root keys that a Worker can
                # fetch directly, so expose the actually published location.
                "object_key": f"{source['version']}/{safe_key}",
                "bytes": artifact["bytes"],
                "sha256": artifact["sha256"],
            }
        references[family] = {
            "source": source,
            "manifest_key": (
                f"{source['version']}/families/{family}/family-manifest.json"
            ),
            "manifest_digest": validated["manifest_digest"],
            "manifest_sha256": _require_sha256(
                manifest_sha, f"{family} manifest SHA-256"
            ),
            "versions": validated["versions"],
            "coverage": validated["region"],
            "operations": operations,
            "entrypoints": normalized_entrypoints,
        }

    _require_consistent_family_source_keys(references)

    manifest = {
        "schema": RELEASE_SCHEMA,
        "geocoder_build": geocoder_build,
        "overture_release": overture_release,
        "generated_at": generated_at,
        "data_version": {
            "overture_release": overture_release,
            "geocoder_build": geocoder_build,
        },
        "legacy_core": {
            "version": legacy["version"],
            "manifest_key": f"{legacy['version']}/release-manifest.json",
            "manifest_sha256": legacy_sha,
            "entrypoints": {
                "feature_lookup": f"{legacy['version']}/id-collection.json",
                "forward": f"{legacy['version']}/collection.json",
                "reverse": f"{legacy['version']}/reverse-collection.json",
            },
        },
        "families": references,
        "operations": _derive_operations(references),
    }
    manifest["release_digest"] = gbm.digest(manifest)
    return manifest


def validate_release_manifest(manifest: Any) -> dict[str, Any]:
    if not isinstance(manifest, dict) or manifest.get("schema") != RELEASE_SCHEMA:
        raise ValueError(f"v2 release schema must be {RELEASE_SCHEMA}")
    _require_exact_fields(
        manifest,
        {
            "schema",
            "geocoder_build",
            "overture_release",
            "generated_at",
            "data_version",
            "legacy_core",
            "families",
            "operations",
            "release_digest",
        },
        "v2 release manifest",
    )
    build = _require_build(manifest["geocoder_build"])
    release = _require_string(manifest["overture_release"], "overture_release")
    _require_string(manifest["generated_at"], "generated_at")
    _require_sha256(manifest["release_digest"], "release_digest")
    if manifest["data_version"] != {
        "overture_release": release,
        "geocoder_build": build,
    }:
        raise ValueError("data_version differs from the release identity")

    legacy = manifest["legacy_core"]
    if not isinstance(legacy, dict):
        raise ValueError("legacy_core must be an object")
    _require_exact_fields(
        legacy,
        {"version", "manifest_key", "manifest_sha256", "entrypoints"},
        "legacy_core",
    )
    legacy_version = _require_string(legacy["version"], "legacy core version")
    expected_legacy_key = f"{legacy_version}/release-manifest.json"
    if _require_safe_key(legacy["manifest_key"], "legacy manifest key") != expected_legacy_key:
        raise ValueError("legacy manifest key differs from its version")
    _require_sha256(legacy["manifest_sha256"], "legacy manifest SHA-256")
    expected_core_entrypoints = {
        "feature_lookup": f"{legacy_version}/id-collection.json",
        "forward": f"{legacy_version}/collection.json",
        "reverse": f"{legacy_version}/reverse-collection.json",
    }
    if legacy["entrypoints"] != expected_core_entrypoints:
        raise ValueError("legacy core entrypoints differ from the core release layout")
    for operation, key in legacy["entrypoints"].items():
        _require_safe_key(key, f"legacy {operation} entrypoint")

    families = manifest["families"]
    if not isinstance(families, dict):
        raise ValueError("families must be an object")
    normalized_references: dict[str, dict[str, Any]] = {}
    for family, reference in families.items():
        if family not in gbm.FAMILIES or not isinstance(reference, dict):
            raise ValueError(f"unsupported v2 family reference: {family}")
        _require_exact_fields(
            reference,
            {
                "source",
                "manifest_key",
                "manifest_digest",
                "manifest_sha256",
                "versions",
                "coverage",
                "operations",
                "entrypoints",
            },
            f"{family} family reference",
        )
        source = reference["source"]
        if not isinstance(source, dict):
            raise ValueError(f"{family} source must be an object")
        _require_exact_fields(
            source,
            {"kind", "version", "manifest_key", "manifest_sha256"},
            f"{family} source",
        )
        if source["kind"] not in {"core_release", "family_slice"}:
            raise ValueError(f"{family} source kind is unsupported")
        source_version = _require_key_component(
            source["version"], f"{family} source version"
        )
        if source["kind"] == "family_slice" and not SLICE_RE.fullmatch(
            source_version
        ):
            raise ValueError(f"{family} slice version must use slice-YYYY-MM-DD.N")
        if source["kind"] == "core_release" and not BUILD_RE.fullmatch(source_version):
            raise ValueError(f"{family} core source version must use YYYY-MM-DD.N")
        expected_source_key = (
            f"{source_version}/slice-manifest.json"
            if source["kind"] == "family_slice"
            else f"{source_version}/release-manifest.json"
        )
        if (
            _require_safe_key(source["manifest_key"], f"{family} source manifest key")
            != expected_source_key
        ):
            raise ValueError(f"{family} source manifest key differs from its version")
        _require_sha256(
            source["manifest_sha256"], f"{family} source manifest SHA-256"
        )
        expected_key = f"{source_version}/families/{family}/family-manifest.json"
        if _require_safe_key(reference["manifest_key"], f"{family} manifest key") != expected_key:
            raise ValueError(f"{family} manifest key differs from its source version")
        _require_sha256(reference["manifest_digest"], f"{family} manifest digest")
        _require_sha256(reference["manifest_sha256"], f"{family} manifest SHA-256")
        if not isinstance(reference["versions"], dict):
            raise ValueError(f"{family} versions must be an object")
        gbm.normalize_manifest_versions(family, reference["versions"])
        gbm.normalize_region(reference["coverage"])
        operations = _normalize_family_operations(family, reference["operations"])
        if operations != reference["operations"]:
            raise ValueError(f"{family} operations are not sorted and unique")
        entrypoints = reference["entrypoints"]
        if not isinstance(entrypoints, dict) or set(entrypoints) != set(operations):
            raise ValueError(
                f"{family} entrypoints must name exactly its advertised operations"
            )
        for operation, identity in entrypoints.items():
            if not isinstance(identity, dict):
                raise ValueError(f"{family} {operation} entrypoint must be an object")
            _require_exact_fields(
                identity,
                {"object_key", "bytes", "sha256"},
                f"{family} {operation} entrypoint",
            )
            _require_published_family_artifact_key(
                identity["object_key"],
                source_version,
                family,
                f"{family} {operation} entrypoint",
            )
            gbm.require_int(
                identity["bytes"], f"{family} {operation} entrypoint bytes", minimum=1
            )
            _require_sha256(
                identity["sha256"], f"{family} {operation} entrypoint SHA-256"
            )
        normalized_references[family] = reference

    _require_consistent_family_source_keys(normalized_references)
    if manifest["operations"] != _derive_operations(normalized_references):
        raise ValueError("top-level operations differ from family capabilities")
    unsigned = {key: value for key, value in manifest.items() if key != "release_digest"}
    if gbm.digest(unsigned) != manifest["release_digest"]:
        raise ValueError("v2 release manifest does not match its release_digest")
    return manifest


def verify_release_sources(
    manifest: Any,
    *,
    legacy_release: Any,
    legacy_manifest_sha256: str,
    family_manifests: dict[str, tuple[Any, str]] | None = None,
    family_source_manifests: dict[str, tuple[Any, str]] | None = None,
) -> dict[str, Any]:
    """Re-prove every release reference against its source manifest bytes.

    ``validate_release_manifest`` proves the v2 object's own canonical shape and
    digest.  This stronger boundary additionally requires the legacy and family
    manifests, binds their file SHA-256 values, and checks that every advertised
    entrypoint is exactly one of the family's hashed artifacts.
    """
    validated = validate_release_manifest(manifest)
    release = validated["overture_release"]
    legacy = _validate_legacy_release(legacy_release, release)
    expected_legacy = {
        "version": legacy["version"],
        "manifest_key": f"{legacy['version']}/release-manifest.json",
        "manifest_sha256": _require_sha256(
            legacy_manifest_sha256, "legacy manifest SHA-256"
        ),
        "entrypoints": {
            "feature_lookup": f"{legacy['version']}/id-collection.json",
            "forward": f"{legacy['version']}/collection.json",
            "reverse": f"{legacy['version']}/reverse-collection.json",
        },
    }
    if validated["legacy_core"] != expected_legacy:
        raise ValueError("v2 release legacy core differs from its source manifest")

    supplied = family_manifests or {}
    if set(supplied) != set(validated["families"]):
        raise ValueError("v2 release family source set differs")
    supplied_family_sources = family_source_manifests or {}
    if set(supplied_family_sources) != set(validated["families"]):
        raise ValueError("v2 release verified family source set differs")
    for family, source in sorted(supplied.items()):
        if not isinstance(source, tuple) or len(source) != 2:
            raise ValueError(f"{family} family input must be (manifest, sha256)")
        source_manifest, source_sha = source
        family_manifest = gbm.validate_family_manifest(source_manifest)
        if family_manifest["family"] != family:
            raise ValueError(f"{family} family manifest declares another family")
        if family_manifest["lineage"]["overture_release"] != release:
            raise ValueError(f"{family} family Overture release differs")
        source_input = supplied_family_sources[family]
        if not isinstance(source_input, tuple) or len(source_input) != 2:
            raise ValueError(f"{family} source input must be (manifest, sha256)")
        family_source = _validate_family_source(
            family,
            source_input[0],
            source_input[1],
            family_manifest,
            release,
        )
        reference = validated["families"][family]
        expected_reference_fields = {
            "source": family_source,
            "manifest_key": (
                f"{family_source['version']}/families/{family}/family-manifest.json"
            ),
            "manifest_digest": family_manifest["manifest_digest"],
            "manifest_sha256": _require_sha256(
                source_sha, f"{family} manifest SHA-256"
            ),
            "versions": family_manifest["versions"],
            "coverage": family_manifest["region"],
        }
        for field, expected in expected_reference_fields.items():
            if reference[field] != expected:
                raise ValueError(
                    f"{family} family reference {field} differs from its source manifest"
                )
        artifacts_by_key = {
            f"{family_source['version']}/{artifact['object_key']}": {
                "object_key": f"{family_source['version']}/{artifact['object_key']}",
                "bytes": artifact["bytes"],
                "sha256": artifact["sha256"],
            }
            for artifact in family_manifest["artifacts"]
        }
        for operation, identity in reference["entrypoints"].items():
            if artifacts_by_key.get(identity["object_key"]) != identity:
                raise ValueError(
                    f"{family} {operation} entrypoint differs from its source artifact"
                )
    return validated


def release_manifest_key_for_catalog(catalog_key: str, geocoder_build: str) -> str:
    """Return the only release-manifest key allowed for one catalog root."""

    geocoder_build = _require_build(geocoder_build)
    catalog_key = _require_safe_key(catalog_key, "v2 catalog key")
    if catalog_key == PRODUCTION_CATALOG_KEY:
        return f"v2/releases/{geocoder_build}/release.json"
    if PREVIEW_CATALOG_RE.fullmatch(catalog_key):
        return f"{catalog_key.rsplit('/', 1)[0]}/release.json"
    raise ValueError(
        "v2 catalog key must be production or a guarded smoketest-v2 run catalog"
    )


def _catalog_entry(
    release: dict[str, Any], manifest_sha256: str, *, catalog_key: str
) -> dict[str, Any]:
    build = release["geocoder_build"]
    return {
        "geocoder_build": build,
        "overture_release": release["overture_release"],
        "manifest_key": release_manifest_key_for_catalog(catalog_key, build),
        "manifest_sha256": _require_sha256(
            manifest_sha256, "v2 release manifest SHA-256"
        ),
        "release_digest": release["release_digest"],
    }


def build_catalog(
    *,
    release_manifest: Any,
    release_manifest_sha256: str,
    legacy_release: Any,
    legacy_manifest_sha256: str,
    family_manifests: dict[str, tuple[Any, str]] | None = None,
    family_source_manifests: dict[str, tuple[Any, str]] | None = None,
    before: Any | None = None,
    initialize: bool = False,
    generated_at: str | None = None,
    catalog_key: str = PRODUCTION_CATALOG_KEY,
) -> dict[str, Any]:
    # Catalog construction is the discovery-root boundary. A self-consistent
    # release digest is insufficient here: re-prove every reference against
    # the source manifests before making the release discoverable.
    release = verify_release_sources(
        release_manifest,
        legacy_release=legacy_release,
        legacy_manifest_sha256=legacy_manifest_sha256,
        family_manifests=family_manifests,
        family_source_manifests=family_source_manifests,
    )
    generated_at = _require_string(generated_at or _now(), "generated_at")
    release_manifest_key_for_catalog(catalog_key, release["geocoder_build"])
    preview = catalog_key != PRODUCTION_CATALOG_KEY
    previous: list[dict[str, Any]] = []
    if (before is None) == (not initialize):
        raise ValueError("catalog build requires exactly one of before or initialize")
    if before is not None:
        if preview:
            raise ValueError("a preview v2 catalog cannot preserve release history")
        previous = validate_catalog(before, catalog_key=catalog_key)["releases"]
        latest = previous[0]["geocoder_build"]
        if version_sort_key(release["geocoder_build"]) <= version_sort_key(latest):
            raise ValueError("new geocoder_build must be newer than catalog latest")
    entries = [
        _catalog_entry(release, release_manifest_sha256, catalog_key=catalog_key),
        *previous,
    ]
    catalog = {
        "schema": CATALOG_SCHEMA,
        "generated_at": generated_at,
        "latest": release["geocoder_build"],
        "releases": entries,
    }
    catalog["catalog_digest"] = gbm.digest(catalog)
    return catalog


def validate_catalog(
    catalog: Any, *, catalog_key: str = PRODUCTION_CATALOG_KEY
) -> dict[str, Any]:
    if not isinstance(catalog, dict) or catalog.get("schema") != CATALOG_SCHEMA:
        raise ValueError(f"v2 catalog schema must be {CATALOG_SCHEMA}")
    _require_exact_fields(
        catalog,
        {"schema", "generated_at", "latest", "releases", "catalog_digest"},
        "v2 catalog",
    )
    _require_string(catalog["generated_at"], "generated_at")
    latest = _require_build(catalog["latest"])
    _require_sha256(catalog["catalog_digest"], "catalog_digest")
    entries = catalog["releases"]
    if not isinstance(entries, list) or not entries:
        raise ValueError("v2 catalog requires at least one release")
    if catalog_key != PRODUCTION_CATALOG_KEY and len(entries) != 1:
        raise ValueError("a preview v2 catalog must contain exactly one release")
    builds: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("v2 catalog release entries must be objects")
        _require_exact_fields(
            entry,
            {
                "geocoder_build",
                "overture_release",
                "manifest_key",
                "manifest_sha256",
                "release_digest",
            },
            "v2 catalog release",
        )
        build = _require_build(entry["geocoder_build"])
        _require_string(entry["overture_release"], "catalog Overture release")
        expected_key = release_manifest_key_for_catalog(catalog_key, build)
        if _require_safe_key(entry["manifest_key"], "v2 release manifest key") != expected_key:
            raise ValueError("v2 release manifest key differs from geocoder_build")
        _require_sha256(entry["manifest_sha256"], "v2 release manifest SHA-256")
        _require_sha256(entry["release_digest"], "v2 release digest")
        builds.append(build)
    if len(builds) != len(set(builds)):
        raise ValueError("v2 catalog contains duplicate geocoder builds")
    if builds != sorted(builds, key=version_sort_key, reverse=True):
        raise ValueError("v2 catalog releases are not newest-first")
    if latest != builds[0]:
        raise ValueError("v2 catalog latest differs from its first release")
    unsigned = {key: value for key, value in catalog.items() if key != "catalog_digest"}
    if gbm.digest(unsigned) != catalog["catalog_digest"]:
        raise ValueError("v2 catalog does not match its catalog_digest")
    return catalog


def build_unavailable_catalog(
    *,
    previous_catalog_sha256: str,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Build the signed production tombstone used instead of unsafe DELETE.

    R2 enforces ``If-Match`` on PutObject but not DeleteObject.  Replacing the
    live catalog with this document therefore preserves the same atomic CAS
    boundary as promotion while giving the worker an explicit unavailable
    state.
    """
    catalog = {
        "schema": UNAVAILABLE_CATALOG_SCHEMA,
        "generated_at": _require_string(generated_at or _now(), "generated_at"),
        "previous_catalog_sha256": _require_sha256(
            previous_catalog_sha256, "previous catalog SHA-256"
        ),
        "reason": UNAVAILABLE_REASON,
    }
    catalog["catalog_digest"] = gbm.digest(catalog)
    return catalog


def validate_unavailable_catalog(
    catalog: Any, *, catalog_key: str = PRODUCTION_CATALOG_KEY
) -> dict[str, Any]:
    if catalog_key != PRODUCTION_CATALOG_KEY:
        raise ValueError("an unavailable v2 catalog is allowed only in production")
    if (
        not isinstance(catalog, dict)
        or catalog.get("schema") != UNAVAILABLE_CATALOG_SCHEMA
    ):
        raise ValueError(
            f"unavailable v2 catalog schema must be {UNAVAILABLE_CATALOG_SCHEMA}"
        )
    _require_exact_fields(
        catalog,
        {
            "schema",
            "generated_at",
            "previous_catalog_sha256",
            "reason",
            "catalog_digest",
        },
        "unavailable v2 catalog",
    )
    _require_string(catalog["generated_at"], "generated_at")
    _require_sha256(
        catalog["previous_catalog_sha256"], "previous catalog SHA-256"
    )
    if catalog["reason"] != UNAVAILABLE_REASON:
        raise ValueError(
            f"unavailable v2 catalog reason must be {UNAVAILABLE_REASON!r}"
        )
    _require_sha256(catalog["catalog_digest"], "catalog_digest")
    unsigned = {
        key: value for key, value in catalog.items() if key != "catalog_digest"
    }
    if gbm.digest(unsigned) != catalog["catalog_digest"]:
        raise ValueError(
            "unavailable v2 catalog does not match its catalog_digest"
        )
    return catalog


# ---------------------------------------------------------------------------
# Publication layer: assemble / publish-release / promote / recover.
#
# Worker admission contract for promoted construction slices, mined from
# crates/geocoder-worker/src/v2.rs validate_family (the worker is FROZEN; this
# producer conforms to it, never the reverse):
#
#   format strings : "PLRV0002+PLHD0002" (places_construction_v1.rs:32) and
#                    "OAV1ART" (address_construction_v1.rs:47), accepted only
#                    on family_slice sources (v2.rs:397-402).
#   tokenizer      : places must carry exactly TOKENIZER_VERSION
#                    "nfkd-lower-stripmark-cjk-bigram-v4" (places_pages.rs:67,
#                    v2.rs:411) and null normalization; addresses the reverse,
#                    with ADDRESS_CONSTRUCTION_NORMALIZATION_VERSION
#                    "address-transform-v1" (address_construction_v1.rs:50,
#                    v2.rs:403-417).
#   operations     : exactly ["forward"] (places) and ["structured_forward"]
#                    (addresses) (v2.rs:410,415).
#   entrypoints    : exactly {slice}/families/{family}/routing.json, non-empty
#                    and within MAX_PLACES_ROUTING_BYTES /
#                    MAX_ADDRESS_ROUTING_BYTES = 8 MiB
#                    (places_construction_v1.rs:41,
#                    address_construction_v1.rs:57, v2.rs:422-457).
WORKER_CONSTRUCTION_CONTRACTS = {
    ("places", "PLRV0002+PLHD0002"): {
        "operations": ("forward",),
        "tokenizer": "nfkd-lower-stripmark-cjk-bigram-v4",
        "normalization": None,
        "entrypoint": "routing.json",
        "entrypoint_cap": 8 * 1024 * 1024,
    },
    ("addresses", "OAV1ART"): {
        "operations": ("structured_forward",),
        "tokenizer": None,
        "normalization": "address-transform-v1",
        "entrypoint": "routing.json",
        "entrypoint_cap": 8 * 1024 * 1024,
    },
}

_ABSENT_CODES = frozenset({"404", "NoSuchKey", "NotFound"})
_CONFLICT_CODES = frozenset({"412", "PreconditionFailed"})


def _fail(message: str) -> SystemExit:
    return SystemExit(f"v2-release-manifest: {message}")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class StateConflict(Exception):
    """The store's current state differs from the stated expectation."""


class LocalControlStore:
    """Filesystem control-document store for rehearsal and tests.

    The compare-and-swap token is the SHA-256 of the current bytes (state IS
    value here).  ``put(expect=token)`` is check-then-act with a small race
    window between reading the current bytes and replacing them; the R2
    backend closes that window with ``If-Match``.
    """

    scheme = "local"

    def __init__(self, root: Path):
        self.root = root

    def _path(self, key: str) -> Path:
        if key.startswith("/") or any(
            part in ("", ".", "..") for part in key.split("/")
        ):
            raise _fail(f"unsafe object key {key!r}")
        return self.root / key

    def get(self, key: str) -> tuple[bytes, str] | None:
        path = self._path(key)
        if not path.is_file():
            return None
        payload = path.read_bytes()
        return payload, _sha256_bytes(payload)

    def put(self, key: str, payload: bytes, *, expect: str | None = None) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as staging:
            staging_path = Path(staging.name)
            staging.write(payload)
        try:
            if expect is None:
                try:
                    # link(2) is the filesystem's atomic create-only PUT.
                    os.link(staging_path, path)
                except FileExistsError as error:
                    raise StateConflict(f"{key} already exists") from error
            else:
                current = self.get(key)
                if current is None or current[1] != expect:
                    raise StateConflict(
                        f"{key} state differs from the stated expectation"
                    )
                os.replace(staging_path, path)
        finally:
            staging_path.unlink(missing_ok=True)

class R2ControlStore:
    """R2 control-document store over r2_verified_store's persistent client.

    ``get`` returns the payload plus the ETag from that same response; ``put``
    with an expectation sends ``If-Match`` with that ETag, so the swap is
    conditional at the store on the exact bytes the expectation hashed (a
    single-PUT ETag is the content MD5, so the condition is value-addressed).
    ``If-Match`` on PutObject needs a one-object live probe before first
    production use; see the module docstring.  R2 does not implement
    conditional DeleteObject, so mutable control-plane recovery never deletes
    the catalog: taking v2 unavailable is another conditional PutObject.
    """

    scheme = "r2"

    def __init__(self, store: Any):
        self.store = store

    def _codes(self, error: Any) -> set[str]:
        response = getattr(error, "response", None) or {}
        metadata = response.get("ResponseMetadata") or {}
        return {
            str((response.get("Error") or {}).get("Code", "")),
            str(metadata.get("HTTPStatusCode", "")),
        }

    def get(self, key: str) -> tuple[bytes, str] | None:
        try:
            payload = self.store.client.get_object(
                Bucket=self.store.bucket, Key=key
            )
        except self.store._client_error as error:
            if self._codes(error) & _ABSENT_CODES:
                return None
            raise RuntimeError(f"get-object failed for {key}: {error}") from error
        body = payload["Body"]
        with contextlib.closing(body):
            data = body.read()
        if len(data) != int(payload["ContentLength"]):
            raise _fail(f"short read for {key}")
        etag = payload.get("ETag")
        if not isinstance(etag, str) or not etag:
            raise _fail(f"object {key} carries no ETag to compare-and-swap on")
        return data, etag

    def put(self, key: str, payload: bytes, *, expect: str | None = None) -> None:
        arguments: dict[str, Any] = {
            "Bucket": self.store.bucket,
            "Key": key,
            "Body": io.BytesIO(payload),
            "ContentLength": len(payload),
            "Metadata": {"sha256": _sha256_bytes(payload)},
        }
        if expect is None:
            arguments["IfNoneMatch"] = "*"
        else:
            arguments["IfMatch"] = expect
        try:
            self.store.client.put_object(**arguments)
        except self.store._client_error as error:
            if self._codes(error) & _CONFLICT_CODES:
                raise StateConflict(
                    f"{key} state differs from the stated expectation"
                ) from error
            raise RuntimeError(f"put-object failed for {key}: {error}") from error

def open_control_store(spec: str, what: str) -> LocalControlStore | R2ControlStore:
    """``local:<absolute-root>`` or ``r2:<bucket>``, reusing the promotion
    tool's store construction and credential gate."""
    import promote_construction_slice as promotion

    tree = promotion.open_tree(spec, what)
    if isinstance(tree, promotion.LocalTree):
        return LocalControlStore(tree.root)
    return R2ControlStore(tree.store)


def _fetch(
    store: LocalControlStore | R2ControlStore, key: str, what: str
) -> tuple[bytes, str]:
    found = store.get(key)
    if found is None:
        raise _fail(f"{what} is missing at {key}")
    return found[0], _sha256_bytes(found[0])


def _fetch_document(
    store: LocalControlStore | R2ControlStore, key: str, what: str
) -> tuple[dict[str, Any], bytes, str]:
    payload, sha = _fetch(store, key, what)
    try:
        value = json.loads(payload)
    except ValueError as error:
        raise _fail(f"{what} at {key} is not valid JSON") from error
    if not isinstance(value, dict):
        raise _fail(f"{what} at {key} is not a JSON object")
    return value, payload, sha


def _load_release_from_store(
    store: LocalControlStore | R2ControlStore, catalog_key: str, build: str
) -> tuple[str, dict[str, Any], str]:
    key = release_manifest_key_for_catalog(catalog_key, build)
    release, _, sha = _fetch_document(store, key, f"v2 release {build}")
    validate_release_manifest(release)
    if release["geocoder_build"] != build:
        raise _fail(
            f"release document at {key} declares build "
            f"{release['geocoder_build']!r}, not {build!r}"
        )
    return key, release, sha


def _release_sources_from_store(
    store: LocalControlStore | R2ControlStore, release: dict[str, Any]
) -> dict[str, Any]:
    """Fetch and hash-verify every source document and entrypoint the release
    pins -- the same objects the worker hashes at admission (minus the sampled
    data objects) -- returning ``build_catalog`` / ``verify_release_sources``
    keyword inputs."""
    legacy_key = release["legacy_core"]["manifest_key"]
    legacy, _, legacy_sha = _fetch_document(
        store, legacy_key, "legacy core release manifest"
    )
    if legacy_sha != release["legacy_core"]["manifest_sha256"]:
        raise _fail(
            f"legacy core manifest bytes at {legacy_key} do not hash to the "
            "release-pinned sha256"
        )
    family_manifests: dict[str, tuple[Any, str]] = {}
    family_sources: dict[str, tuple[Any, str]] = {}
    for family, reference in sorted(release["families"].items()):
        source_key = reference["source"]["manifest_key"]
        source, _, source_sha = _fetch_document(
            store, source_key, f"{family} source manifest"
        )
        if source_sha != reference["source"]["manifest_sha256"]:
            raise _fail(
                f"{family} source manifest bytes at {source_key} do not hash "
                "to the release-pinned sha256"
            )
        manifest, _, manifest_sha = _fetch_document(
            store, reference["manifest_key"], f"{family} family manifest"
        )
        if manifest_sha != reference["manifest_sha256"]:
            raise _fail(
                f"{family} family manifest bytes at {reference['manifest_key']} "
                "do not hash to the release-pinned sha256"
            )
        for operation, identity in sorted(reference["entrypoints"].items()):
            payload, payload_sha = _fetch(
                store, identity["object_key"], f"{family} {operation} entrypoint"
            )
            if len(payload) != identity["bytes"] or payload_sha != identity["sha256"]:
                raise _fail(
                    f"{family} {operation} entrypoint at "
                    f"{identity['object_key']} does not match its "
                    "release-pinned identity"
                )
        family_manifests[family] = (manifest, manifest_sha)
        family_sources[family] = (source, source_sha)
    return {
        "legacy_release": legacy,
        "legacy_manifest_sha256": legacy_sha,
        "family_manifests": family_manifests,
        "family_source_manifests": family_sources,
    }


def _write_catalog_verified(
    store: LocalControlStore | R2ControlStore,
    catalog_key: str,
    payload: bytes,
    expect_token: str | None,
) -> None:
    try:
        store.put(catalog_key, payload, expect=expect_token)
    except StateConflict as error:
        raise _fail(
            f"catalog {catalog_key} changed underneath the compare-and-swap; "
            "re-read it and restate the expectation"
        ) from error
    # Verify-after-write: re-download and hash what a reader will fetch.
    written = store.get(catalog_key)
    if written is None or _sha256_bytes(written[0]) != _sha256_bytes(payload):
        raise _fail(f"post-write verification failed for {catalog_key}")


def cmd_assemble(args: argparse.Namespace) -> None:
    build = _require_build(args.geocoder_build)
    if not SLICE_RE.fullmatch(args.slice_version):
        raise _fail("--slice-version must use slice-YYYY-MM-DD.N")
    legacy_version = _require_build(args.legacy_core)
    families = sorted(set(args.family or sorted(gbm.FAMILIES)))
    store = open_control_store(args.store, "--store")

    legacy, _, legacy_sha = _fetch_document(
        store,
        f"{legacy_version}/release-manifest.json",
        "legacy core release manifest",
    )
    source_key = f"{args.slice_version}/slice-manifest.json"
    source, _, source_sha = _fetch_document(store, source_key, "slice source manifest")
    if source.get("slice_version") != args.slice_version:
        raise _fail(
            f"slice source manifest at {source_key} declares slice_version "
            f"{source.get('slice_version')!r}, not {args.slice_version!r}"
        )

    family_manifests: dict[str, tuple[Any, str]] = {}
    family_source_manifests: dict[str, tuple[Any, str]] = {}
    operations: dict[str, list[str]] = {}
    entrypoints: dict[str, dict[str, str]] = {}
    for family in families:
        manifest_key = f"{args.slice_version}/families/{family}/family-manifest.json"
        manifest, _, manifest_sha = _fetch_document(
            store, manifest_key, f"{family} family manifest"
        )
        validated = gbm.validate_family_manifest(manifest)
        fmt = validated["versions"]["format"]
        contract = WORKER_CONSTRUCTION_CONTRACTS.get((family, fmt))
        if contract is None:
            raise _fail(
                f"{family} format {fmt!r} is not a promoted construction "
                "format the worker admits on a family_slice source; use the "
                "`release` subcommand for legacy layouts"
            )
        if (
            validated["versions"]["tokenizer"] != contract["tokenizer"]
            or validated["versions"]["normalization"] != contract["normalization"]
        ):
            raise _fail(
                f"{family} tokenizer/normalization differ from the worker "
                "admission contract"
            )
        entrypoint_key = f"families/{family}/{contract['entrypoint']}"
        recorded = next(
            (
                artifact
                for artifact in validated["artifacts"]
                if artifact["object_key"] == entrypoint_key
            ),
            None,
        )
        if recorded is None:
            raise _fail(f"{family} family manifest does not attest {entrypoint_key}")
        # The worker pins the entrypoint identity from the release document and
        # verifies the STORED bytes against it at admission; read those bytes
        # now so the release can only ever pin what is actually there.
        payload, payload_sha = _fetch(
            store,
            f"{args.slice_version}/{entrypoint_key}",
            f"{family} routing entrypoint",
        )
        if len(payload) != recorded["bytes"] or payload_sha != recorded["sha256"]:
            raise _fail(
                f"{family} routing entrypoint bytes do not match the identity "
                "its family manifest records"
            )
        if not payload or len(payload) > contract["entrypoint_cap"]:
            raise _fail(
                f"{family} routing entrypoint is outside the worker's "
                f"(0, {contract['entrypoint_cap']}] byte cap"
            )
        family_manifests[family] = (manifest, manifest_sha)
        family_source_manifests[family] = (source, source_sha)
        operations[family] = list(contract["operations"])
        entrypoints[family] = {
            operation: entrypoint_key for operation in contract["operations"]
        }

    # Deterministic by default: two assembles of the same inputs are
    # byte-identical. generated_at is a label the worker hashes but ignores.
    generated_at = args.generated_at or f"{build[:10]}T00:00:00+00:00"
    release = build_release_manifest(
        geocoder_build=build,
        overture_release=args.overture_release,
        legacy_release=legacy,
        legacy_manifest_sha256=legacy_sha,
        family_manifests=family_manifests,
        family_source_manifests=family_source_manifests,
        family_operations=operations,
        family_entrypoints=entrypoints,
        generated_at=generated_at,
    )
    for family, reference in release["families"].items():
        if reference["source"]["version"] != args.slice_version:
            raise _fail(
                f"{family} source version {reference['source']['version']!r} "
                f"differs from the requested slice {args.slice_version!r}"
            )
    payload = gbm.canonical_json(release)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(payload)
    print(
        json.dumps(
            {
                "assembled": str(args.output),
                "geocoder_build": build,
                "release_digest": release["release_digest"],
                "sha256": _sha256_bytes(payload),
                "bytes": len(payload),
                "manifest_key": release_manifest_key_for_catalog(
                    args.catalog_key, build
                ),
            },
            sort_keys=True,
        )
    )


def cmd_publish_release(args: argparse.Namespace) -> None:
    payload = Path(args.release).read_bytes()
    try:
        release = json.loads(payload)
    except ValueError as error:
        raise _fail(f"{args.release} is not valid JSON") from error
    validate_release_manifest(release)
    key = release_manifest_key_for_catalog(args.catalog_key, release["geocoder_build"])
    sha = _sha256_bytes(payload)
    print(
        json.dumps(
            {
                "planned_write": {
                    "key": key,
                    "sha256": sha,
                    "bytes": len(payload),
                    "mode": "create-only",
                }
            },
            sort_keys=True,
        )
    )
    store = open_control_store(args.store, "--store")
    existing = store.get(key)
    if existing is not None:
        if _sha256_bytes(existing[0]) == sha:
            print(
                json.dumps(
                    {"published": key, "sha256": sha, "status": "already-published"},
                    sort_keys=True,
                )
            )
            return
        raise _fail(
            f"release document {key} exists with different bytes (sha256 "
            f"{_sha256_bytes(existing[0])}); release documents are immutable "
            "-- assemble under a new build id"
        )
    if not args.execute:
        print(
            json.dumps(
                {"published": None, "sha256": sha, "status": "dry-run"},
                sort_keys=True,
            )
        )
        return
    try:
        store.put(key, payload, expect=None)
    except StateConflict as error:
        # A concurrent create won after our read: byte-identical is a benign
        # race, anything else is a squatter.
        raced = store.get(key)
        if raced is None or _sha256_bytes(raced[0]) != sha:
            raise _fail(
                f"release document {key} appeared with different bytes during "
                "the create-only write"
            ) from error
    written = store.get(key)
    if written is None or _sha256_bytes(written[0]) != sha:
        raise _fail(f"post-write verification failed for {key}")
    print(
        json.dumps(
            {"published": key, "sha256": sha, "status": "written"}, sort_keys=True
        )
    )


def cmd_promote(args: argparse.Namespace) -> None:
    if (args.expect_sha256 is None) == (not args.expect_absent):
        raise _fail("promote requires exactly one of --expect-absent or --expect-sha256")
    if args.expect_sha256 is not None and not SHA256_RE.fullmatch(args.expect_sha256):
        raise _fail("--expect-sha256 must be a lowercase SHA-256 digest")
    store = open_control_store(args.store, "--store")
    _, release, release_sha = _load_release_from_store(
        store, args.catalog_key, args.build
    )
    sources = _release_sources_from_store(store, release)
    current = store.get(args.catalog_key)
    if args.expect_absent:
        if current is not None:
            raise _fail(
                f"catalog {args.catalog_key} exists (sha256 "
                f"{_sha256_bytes(current[0])}); review it and rerun with "
                "--expect-sha256"
            )
        expect_token = None
        before = None
    else:
        if current is None:
            raise _fail(
                f"catalog {args.catalog_key} is absent; rerun with "
                "--expect-absent if this is the first promotion"
            )
        current_sha = _sha256_bytes(current[0])
        if current_sha != args.expect_sha256:
            raise _fail(
                f"catalog {args.catalog_key} is sha256 {current_sha}, not the "
                "stated expectation; refusing the compare-and-swap"
            )
        expect_token = current[1]
        try:
            before = json.loads(current[0])
        except ValueError as error:
            raise _fail(
                f"catalog {args.catalog_key} is not valid JSON; use recover"
            ) from error
        if (
            isinstance(before, dict)
            and before.get("schema") == UNAVAILABLE_CATALOG_SCHEMA
        ):
            try:
                validate_unavailable_catalog(
                    before, catalog_key=args.catalog_key
                )
            except ValueError as error:
                raise _fail(
                    f"catalog {args.catalog_key} is not a valid unavailable "
                    f"document: {error}"
                ) from error
            # An unavailable document has no release history to preserve, but
            # the replacement still uses the ETag from the same GET above.
            before = None
    catalog = build_catalog(
        release_manifest=release,
        release_manifest_sha256=release_sha,
        before=before,
        initialize=before is None,
        generated_at=args.generated_at,
        catalog_key=args.catalog_key,
        **sources,
    )
    payload = gbm.canonical_json(catalog)
    sha = _sha256_bytes(payload)
    print(
        json.dumps(
            {
                "planned_write": {
                    "key": args.catalog_key,
                    "sha256": sha,
                    "bytes": len(payload),
                    "latest": args.build,
                    "mode": "create-only" if expect_token is None else "compare-and-swap",
                }
            },
            sort_keys=True,
        )
    )
    if not args.execute:
        print(json.dumps({"promoted": None, "status": "dry-run"}, sort_keys=True))
        return
    _write_catalog_verified(store, args.catalog_key, payload, expect_token)
    print(
        json.dumps(
            {
                "catalog": args.catalog_key,
                "promoted": args.build,
                "sha256": sha,
                "status": "written",
            },
            sort_keys=True,
        )
    )


def cmd_recover(args: argparse.Namespace) -> None:
    if (args.build is None) == (not args.unavailable):
        raise _fail("recover requires exactly one of --build or --unavailable")
    if not SHA256_RE.fullmatch(args.expect_sha256):
        raise _fail("--expect-sha256 must be a lowercase SHA-256 digest")
    store = open_control_store(args.store, "--store")
    current = store.get(args.catalog_key)
    if current is None:
        raise _fail(
            f"catalog {args.catalog_key} is absent; nothing to recover "
            "(a first promotion is `promote --expect-absent`)"
        )
    current_sha = _sha256_bytes(current[0])
    if current_sha != args.expect_sha256:
        raise _fail(
            f"catalog {args.catalog_key} is sha256 {current_sha}, not the "
            "stated expectation; refusing to act"
        )
    expect_token = current[1]

    if args.unavailable:
        if args.catalog_key != PRODUCTION_CATALOG_KEY:
            raise _fail("--unavailable is allowed only for v2/catalog.json")
        unavailable = build_unavailable_catalog(
            previous_catalog_sha256=current_sha,
            generated_at=args.generated_at,
        )
        payload = gbm.canonical_json(unavailable)
        sha = _sha256_bytes(payload)
        print(
            json.dumps(
                {
                    "planned_write": {
                        "key": args.catalog_key,
                        "sha256": sha,
                        "bytes": len(payload),
                        "previous_catalog_sha256": current_sha,
                        "state": "unavailable",
                        "mode": "compare-and-swap",
                    }
                },
                sort_keys=True,
            )
        )
        if not args.execute:
            print(
                json.dumps(
                    {"recovered": None, "sha256": sha, "status": "dry-run"},
                    sort_keys=True,
                )
            )
            return
        _write_catalog_verified(store, args.catalog_key, payload, expect_token)
        # The worker serves 503 release_unavailable until a CAS promotion or
        # recovery to a named immutable release replaces this document.
        print(
            json.dumps(
                {
                    "catalog": args.catalog_key,
                    "recovered": "unavailable",
                    "sha256": sha,
                    "status": "written",
                },
                sort_keys=True,
            )
        )
        return

    _, release, release_sha = _load_release_from_store(
        store, args.catalog_key, args.build
    )
    sources = _release_sources_from_store(store, release)
    verify_release_sources(release, **sources)
    history = "preserved"
    retained: list[dict[str, Any]] = []
    try:
        before = validate_catalog(
            json.loads(current[0]), catalog_key=args.catalog_key
        )
        target = version_sort_key(args.build)
        retained = [
            entry
            for entry in before["releases"]
            if version_sort_key(entry["geocoder_build"]) < target
        ]
    except ValueError:
        # The current catalog does not validate -- which may be exactly why we
        # are recovering. Repoint with a single-entry catalog rather than
        # blocking the rollback on the damage being rolled back.
        history = "discarded-invalid"
    catalog: dict[str, Any] = {
        "schema": CATALOG_SCHEMA,
        "generated_at": _require_string(args.generated_at or _now(), "generated_at"),
        "latest": args.build,
        "releases": [
            _catalog_entry(release, release_sha, catalog_key=args.catalog_key),
            *retained,
        ],
    }
    catalog["catalog_digest"] = gbm.digest(catalog)
    validate_catalog(catalog, catalog_key=args.catalog_key)
    payload = gbm.canonical_json(catalog)
    sha = _sha256_bytes(payload)
    print(
        json.dumps(
            {
                "planned_write": {
                    "key": args.catalog_key,
                    "sha256": sha,
                    "bytes": len(payload),
                    "latest": args.build,
                    "history": history,
                    "mode": "compare-and-swap",
                }
            },
            sort_keys=True,
        )
    )
    if not args.execute:
        print(json.dumps({"recovered": None, "status": "dry-run"}, sort_keys=True))
        return
    _write_catalog_verified(store, args.catalog_key, payload, expect_token)
    print(
        json.dumps(
            {
                "catalog": args.catalog_key,
                "recovered": args.build,
                "sha256": sha,
                "status": "written",
            },
            sort_keys=True,
        )
    )


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def _parse_assignment(value: str, kind: str) -> tuple[str, str]:
    family, separator, assigned = value.partition("=")
    if not separator or not family or not assigned:
        raise ValueError(f"{kind} must use FAMILY=VALUE")
    return family, assigned


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    release = commands.add_parser("release", help="build a v2 release manifest")
    release.add_argument("--geocoder-build", required=True)
    release.add_argument("--overture-release", required=True)
    release.add_argument("--legacy-release-manifest", type=Path, required=True)
    release.add_argument("--family-manifest", action="append", default=[])
    release.add_argument("--family-source-manifest", action="append", default=[])
    release.add_argument("--operation", action="append", default=[])
    release.add_argument("--entrypoint", action="append", default=[])
    release.add_argument("--generated-at")
    release.add_argument("--output", type=Path, required=True)

    validate_release = commands.add_parser("validate-release")
    validate_release.add_argument("--manifest", type=Path, required=True)
    validate_release.add_argument("--legacy-release-manifest", type=Path, required=True)
    validate_release.add_argument("--family-manifest", action="append", default=[])
    validate_release.add_argument(
        "--family-source-manifest", action="append", default=[]
    )

    catalog = commands.add_parser("catalog", help="build a v2 catalog candidate")
    catalog.add_argument("--release-manifest", type=Path, required=True)
    catalog.add_argument("--legacy-release-manifest", type=Path, required=True)
    catalog.add_argument("--family-manifest", action="append", default=[])
    catalog.add_argument("--family-source-manifest", action="append", default=[])
    catalog_mode = catalog.add_mutually_exclusive_group(required=True)
    catalog_mode.add_argument("--before", type=Path)
    catalog_mode.add_argument("--initialize", action="store_true")
    catalog.add_argument("--generated-at")
    catalog.add_argument("--catalog-key", default=PRODUCTION_CATALOG_KEY)
    catalog.add_argument("--output", type=Path, required=True)

    validate_catalog_parser = commands.add_parser("validate-catalog")
    validate_catalog_parser.add_argument("--catalog", type=Path, required=True)
    validate_catalog_parser.add_argument(
        "--catalog-key", default=PRODUCTION_CATALOG_KEY
    )

    assemble = commands.add_parser(
        "assemble",
        help="assemble a v2 release from a promoted construction slice",
    )
    assemble.add_argument(
        "--store", required=True, help="local:<absolute-root> or r2:<bucket>"
    )
    assemble.add_argument("--geocoder-build", required=True)
    assemble.add_argument("--overture-release", required=True)
    assemble.add_argument(
        "--slice-version", required=True, help="promoted slice-YYYY-MM-DD.N prefix"
    )
    assemble.add_argument(
        "--legacy-core",
        required=True,
        help="frozen legacy core version, e.g. 2026-07-18.0",
    )
    assemble.add_argument(
        "--family", action="append", choices=sorted(gbm.FAMILIES)
    )
    assemble.add_argument(
        "--generated-at",
        help="defaults to the build date at midnight UTC so two assembles of "
        "the same inputs are byte-identical",
    )
    assemble.add_argument("--catalog-key", default=PRODUCTION_CATALOG_KEY)
    assemble.add_argument("--output", type=Path, required=True)

    publish = commands.add_parser(
        "publish-release",
        help="create-only upload of v2/releases/{build}/release.json",
    )
    publish.add_argument("--store", required=True)
    publish.add_argument("--release", type=Path, required=True)
    publish.add_argument("--catalog-key", default=PRODUCTION_CATALOG_KEY)
    publish.add_argument("--execute", action="store_true")

    promote = commands.add_parser(
        "promote",
        help="compare-and-swap v2/catalog.json to a published release",
    )
    promote.add_argument("--store", required=True)
    promote.add_argument("--build", required=True)
    promote.add_argument("--catalog-key", default=PRODUCTION_CATALOG_KEY)
    promote.add_argument("--expect-absent", action="store_true")
    promote.add_argument("--expect-sha256")
    promote.add_argument("--generated-at")
    promote.add_argument("--execute", action="store_true")

    recover = commands.add_parser(
        "recover",
        help="repoint the catalog at a prior release, or make v2 unavailable",
    )
    recover.add_argument("--store", required=True)
    recover.add_argument("--build")
    recover.add_argument("--unavailable", action="store_true")
    recover.add_argument("--catalog-key", default=PRODUCTION_CATALOG_KEY)
    recover.add_argument("--expect-sha256", required=True)
    recover.add_argument("--generated-at")
    recover.add_argument("--execute", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "assemble":
        cmd_assemble(args)
    elif args.command == "publish-release":
        cmd_publish_release(args)
    elif args.command == "promote":
        cmd_promote(args)
    elif args.command == "recover":
        cmd_recover(args)
    elif args.command == "release":
        family_manifests: dict[str, tuple[Any, str]] = {}
        for raw in args.family_manifest:
            family, path_value = _parse_assignment(raw, "family manifest")
            if family in family_manifests:
                raise ValueError(f"duplicate family manifest: {family}")
            path = Path(path_value)
            family_manifests[family] = (_read_json(path), sha256_file(path))
        family_source_manifests: dict[str, tuple[Any, str]] = {}
        for raw in args.family_source_manifest:
            family, path_value = _parse_assignment(raw, "family source manifest")
            if family in family_source_manifests:
                raise ValueError(f"duplicate family source manifest: {family}")
            path = Path(path_value)
            family_source_manifests[family] = (_read_json(path), sha256_file(path))
        operations: dict[str, list[str]] = {}
        for raw in args.operation:
            family, operation = _parse_assignment(raw, "operation")
            operations.setdefault(family, []).append(operation)
        entrypoints: dict[str, dict[str, str]] = {}
        for raw in args.entrypoint:
            family_operation, key = _parse_assignment(raw, "entrypoint")
            family, separator, operation = family_operation.partition(".")
            if not separator or not family or not operation:
                raise ValueError("entrypoint must use FAMILY.OPERATION=OBJECT_KEY")
            family_values = entrypoints.setdefault(family, {})
            if operation in family_values:
                raise ValueError(f"duplicate entrypoint: {family}.{operation}")
            family_values[operation] = key
        legacy_path = args.legacy_release_manifest
        result = build_release_manifest(
            geocoder_build=args.geocoder_build,
            overture_release=args.overture_release,
            legacy_release=_read_json(legacy_path),
            legacy_manifest_sha256=sha256_file(legacy_path),
            family_manifests=family_manifests,
            family_source_manifests=family_source_manifests,
            family_operations=operations,
            family_entrypoints=entrypoints,
            generated_at=args.generated_at,
        )
        gbm.write_json(args.output, result)
    elif args.command == "validate-release":
        family_manifests = {}
        for raw in args.family_manifest:
            family, path_value = _parse_assignment(raw, "family manifest")
            if family in family_manifests:
                raise ValueError(f"duplicate family manifest: {family}")
            path = Path(path_value)
            family_manifests[family] = (_read_json(path), sha256_file(path))
        family_source_manifests = {}
        for raw in args.family_source_manifest:
            family, path_value = _parse_assignment(raw, "family source manifest")
            if family in family_source_manifests:
                raise ValueError(f"duplicate family source manifest: {family}")
            path = Path(path_value)
            family_source_manifests[family] = (_read_json(path), sha256_file(path))
        legacy_path = args.legacy_release_manifest
        result = verify_release_sources(
            _read_json(args.manifest),
            legacy_release=_read_json(legacy_path),
            legacy_manifest_sha256=sha256_file(legacy_path),
            family_manifests=family_manifests,
            family_source_manifests=family_source_manifests,
        )
        print(json.dumps({"status": "ok", "release_digest": result["release_digest"]}))
    elif args.command == "catalog":
        release_payload = _read_json(args.release_manifest)
        family_manifests = {}
        for raw in args.family_manifest:
            family, path_value = _parse_assignment(raw, "family manifest")
            if family in family_manifests:
                raise ValueError(f"duplicate family manifest: {family}")
            path = Path(path_value)
            family_manifests[family] = (_read_json(path), sha256_file(path))
        family_source_manifests = {}
        for raw in args.family_source_manifest:
            family, path_value = _parse_assignment(raw, "family source manifest")
            if family in family_source_manifests:
                raise ValueError(f"duplicate family source manifest: {family}")
            path = Path(path_value)
            family_source_manifests[family] = (_read_json(path), sha256_file(path))
        legacy_path = args.legacy_release_manifest
        result = build_catalog(
            release_manifest=release_payload,
            release_manifest_sha256=sha256_file(args.release_manifest),
            legacy_release=_read_json(legacy_path),
            legacy_manifest_sha256=sha256_file(legacy_path),
            family_manifests=family_manifests,
            family_source_manifests=family_source_manifests,
            before=_read_json(args.before) if args.before else None,
            initialize=args.initialize,
            generated_at=args.generated_at,
            catalog_key=args.catalog_key,
        )
        gbm.write_json(args.output, result)
    else:
        result = validate_catalog(_read_json(args.catalog), catalog_key=args.catalog_key)
        print(json.dumps({"status": "ok", "catalog_digest": result["catalog_digest"]}))


if __name__ == "__main__":
    main()
