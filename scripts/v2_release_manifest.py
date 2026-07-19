#!/usr/bin/env python3
"""Build and validate the non-promoting v2 release/catalog control plane.

The public v1 catalog remains the production discovery root.  This module
creates a separate, deterministic v2 release manifest which binds one complete
legacy core release (division forward/reverse plus the ID index) to optional
Places and address family manifests from that same Overture release.  It also
creates a ``v2/catalog.json`` candidate.  Neither command accesses R2 or
publishes a catalog.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
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
BUILD_RE = re.compile(r"\d{4}-\d{2}-\d{2}\.\d+")
KEY_COMPONENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
FAMILY_OPERATIONS = {
    "addresses": {"forward", "reverse", "structured_forward"},
    "places": {"forward", "reverse"},
}
DEFAULT_FAMILY_OPERATIONS = {
    "addresses": ["structured_forward"],
    "places": ["forward"],
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


def _validate_legacy_release(manifest: Any, overture_release: str) -> dict[str, Any]:
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("legacy release manifest must use schema_version 1")
    version = _require_key_component(
        manifest.get("version"), "legacy release version"
    )
    if manifest.get("overture_release") != overture_release:
        raise ValueError("legacy release Overture release differs")
    families = manifest.get("families")
    if not isinstance(families, dict) or not {"forward", "reverse", "id"}.issubset(families):
        raise ValueError("legacy release must contain forward, reverse, and id families")
    return {"version": version}


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


def build_release_manifest(
    *,
    geocoder_build: str,
    overture_release: str,
    legacy_release: Any,
    legacy_manifest_sha256: str,
    family_manifests: dict[str, tuple[Any, str]] | None = None,
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
                "object_key": safe_key,
                "bytes": artifact["bytes"],
                "sha256": artifact["sha256"],
            }
        references[family] = {
            "manifest_key": (
                f"{legacy['version']}/families/{family}/family-manifest.json"
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
        expected_key = f"{legacy_version}/families/{family}/family-manifest.json"
        if _require_safe_key(reference["manifest_key"], f"{family} manifest key") != expected_key:
            raise ValueError(f"{family} manifest key differs from the legacy version")
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
            _require_family_artifact_key(
                identity["object_key"], family, f"{family} {operation} entrypoint"
            )
            gbm.require_int(
                identity["bytes"], f"{family} {operation} entrypoint bytes", minimum=1
            )
            _require_sha256(
                identity["sha256"], f"{family} {operation} entrypoint SHA-256"
            )
        normalized_references[family] = reference

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
    for family, source in sorted(supplied.items()):
        if not isinstance(source, tuple) or len(source) != 2:
            raise ValueError(f"{family} family input must be (manifest, sha256)")
        source_manifest, source_sha = source
        family_manifest = gbm.validate_family_manifest(source_manifest)
        if family_manifest["family"] != family:
            raise ValueError(f"{family} family manifest declares another family")
        if family_manifest["lineage"]["overture_release"] != release:
            raise ValueError(f"{family} family Overture release differs")
        reference = validated["families"][family]
        expected_reference_fields = {
            "manifest_key": (
                f"{legacy['version']}/families/{family}/family-manifest.json"
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
            artifact["object_key"]: artifact
            for artifact in family_manifest["artifacts"]
        }
        for operation, identity in reference["entrypoints"].items():
            if artifacts_by_key.get(identity["object_key"]) != identity:
                raise ValueError(
                    f"{family} {operation} entrypoint differs from its source artifact"
                )
    return validated


def _catalog_entry(release: dict[str, Any], manifest_sha256: str) -> dict[str, Any]:
    build = release["geocoder_build"]
    return {
        "geocoder_build": build,
        "overture_release": release["overture_release"],
        "manifest_key": f"v2/releases/{build}/release.json",
        "manifest_sha256": _require_sha256(
            manifest_sha256, "v2 release manifest SHA-256"
        ),
        "release_digest": release["release_digest"],
    }


def build_catalog(
    *,
    release_manifest: Any,
    release_manifest_sha256: str,
    before: Any | None = None,
    initialize: bool = False,
    generated_at: str | None = None,
) -> dict[str, Any]:
    release = validate_release_manifest(release_manifest)
    generated_at = _require_string(generated_at or _now(), "generated_at")
    previous: list[dict[str, Any]] = []
    if (before is None) == (not initialize):
        raise ValueError("catalog build requires exactly one of before or initialize")
    if before is not None:
        previous = validate_catalog(before)["releases"]
        latest = previous[0]["geocoder_build"]
        if version_sort_key(release["geocoder_build"]) <= version_sort_key(latest):
            raise ValueError("new geocoder_build must be newer than catalog latest")
    entries = [_catalog_entry(release, release_manifest_sha256), *previous]
    catalog = {
        "schema": CATALOG_SCHEMA,
        "generated_at": generated_at,
        "latest": release["geocoder_build"],
        "releases": entries,
    }
    catalog["catalog_digest"] = gbm.digest(catalog)
    return catalog


def validate_catalog(catalog: Any) -> dict[str, Any]:
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
        expected_key = f"v2/releases/{build}/release.json"
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


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def _parse_assignment(value: str, kind: str) -> tuple[str, str]:
    family, separator, assigned = value.partition("=")
    if not separator or not family or not assigned:
        raise ValueError(f"{kind} must use FAMILY=VALUE")
    return family, assigned


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    release = commands.add_parser("release", help="build a v2 release manifest")
    release.add_argument("--geocoder-build", required=True)
    release.add_argument("--overture-release", required=True)
    release.add_argument("--legacy-release-manifest", type=Path, required=True)
    release.add_argument("--family-manifest", action="append", default=[])
    release.add_argument("--operation", action="append", default=[])
    release.add_argument("--entrypoint", action="append", default=[])
    release.add_argument("--generated-at")
    release.add_argument("--output", type=Path, required=True)

    validate_release = commands.add_parser("validate-release")
    validate_release.add_argument("--manifest", type=Path, required=True)
    validate_release.add_argument("--legacy-release-manifest", type=Path, required=True)
    validate_release.add_argument("--family-manifest", action="append", default=[])

    catalog = commands.add_parser("catalog", help="build a v2 catalog candidate")
    catalog.add_argument("--release-manifest", type=Path, required=True)
    catalog_mode = catalog.add_mutually_exclusive_group(required=True)
    catalog_mode.add_argument("--before", type=Path)
    catalog_mode.add_argument("--initialize", action="store_true")
    catalog.add_argument("--generated-at")
    catalog.add_argument("--output", type=Path, required=True)

    validate_catalog_parser = commands.add_parser("validate-catalog")
    validate_catalog_parser.add_argument("--catalog", type=Path, required=True)

    args = parser.parse_args()
    if args.command == "release":
        family_manifests: dict[str, tuple[Any, str]] = {}
        for raw in args.family_manifest:
            family, path_value = _parse_assignment(raw, "family manifest")
            if family in family_manifests:
                raise ValueError(f"duplicate family manifest: {family}")
            path = Path(path_value)
            family_manifests[family] = (_read_json(path), sha256_file(path))
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
        legacy_path = args.legacy_release_manifest
        result = verify_release_sources(
            _read_json(args.manifest),
            legacy_release=_read_json(legacy_path),
            legacy_manifest_sha256=sha256_file(legacy_path),
            family_manifests=family_manifests,
        )
        print(json.dumps({"status": "ok", "release_digest": result["release_digest"]}))
    elif args.command == "catalog":
        release_payload = _read_json(args.release_manifest)
        result = build_catalog(
            release_manifest=release_payload,
            release_manifest_sha256=sha256_file(args.release_manifest),
            before=_read_json(args.before) if args.before else None,
            initialize=args.initialize,
            generated_at=args.generated_at,
        )
        gbm.write_json(args.output, result)
    else:
        result = validate_catalog(_read_json(args.catalog))
        print(json.dumps({"status": "ok", "catalog_digest": result["catalog_digest"]}))


if __name__ == "__main__":
    main()
