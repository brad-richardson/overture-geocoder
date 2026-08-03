#!/usr/bin/env python3
"""Build a preview release by replacing one family in the live v2 release.

The preview keeps unaffected families on their already-published immutable
sources and replaces exactly one family with a newly promoted candidate slice.
External operations from retained families are omitted: a preview is a forward
quality gate, and it must not silently rebind an external reverse publication.
The resulting release is still verified against every supplied source document
before it is written.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import global_build_manifest as gbm
import v2_release_manifest as v2


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_document(path: Path, what: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"{what} is not readable JSON: {path}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{what} must be a JSON object: {path}")
    return value


def parse_family_paths(values: list[str], what: str) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        family, separator, raw_path = value.partition("=")
        if (
            not separator
            or family not in gbm.FAMILIES
            or not raw_path
            or family in result
        ):
            raise ValueError(
                f"{what} must name each supported family once as FAMILY=PATH"
            )
        result[family] = Path(raw_path)
    return result


def _relative_entrypoint(reference: dict[str, Any], operation: str) -> str:
    version = reference["source"]["version"]
    object_key = reference["entrypoints"][operation]["object_key"]
    prefix = f"{version}/"
    if not object_key.startswith(prefix):
        raise ValueError(
            f"{operation} entrypoint {object_key!r} is outside source {version!r}"
        )
    return object_key[len(prefix) :]


def build_overlay_release(
    *,
    base_release: dict[str, Any],
    legacy_release: dict[str, Any],
    legacy_sha256: str,
    base_family_manifests: dict[str, tuple[dict[str, Any], str]],
    base_family_sources: dict[str, tuple[dict[str, Any], str]],
    candidate_family: str,
    candidate_manifest: dict[str, Any],
    candidate_manifest_sha256: str,
    candidate_source: dict[str, Any],
    candidate_source_sha256: str,
    geocoder_build: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    base = v2.validate_release_manifest(base_release)
    if candidate_family not in gbm.FAMILIES:
        raise ValueError(f"unsupported candidate family: {candidate_family}")
    if candidate_family not in base["families"]:
        raise ValueError("candidate family is absent from the base release")

    expected_retained = set(base["families"]) - {candidate_family}
    if set(base_family_manifests) != expected_retained:
        raise ValueError("retained family manifests differ from the base release")
    if set(base_family_sources) != expected_retained:
        raise ValueError("retained family source manifests differ from the base release")

    # Re-prove the immutable documents retained from the live release before
    # using any of them as preview inputs.
    # The replaced family is deliberately unavailable here; verify each
    # retained reference directly against its pinned hashes instead.
    for family in sorted(expected_retained):
        reference = base["families"][family]
        manifest, manifest_sha = base_family_manifests[family]
        source, source_sha = base_family_sources[family]
        if manifest_sha != reference["manifest_sha256"]:
            raise ValueError(f"{family} retained manifest SHA-256 differs")
        if source_sha != reference["source"]["manifest_sha256"]:
            raise ValueError(f"{family} retained source SHA-256 differs")
        validated_manifest = gbm.validate_family_manifest(manifest)
        if validated_manifest["manifest_digest"] != reference["manifest_digest"]:
            raise ValueError(f"{family} retained manifest digest differs")
        v2._validate_family_source(
            family, source, source_sha, validated_manifest, base["overture_release"]
        )

    candidate = gbm.validate_family_manifest(candidate_manifest)
    if candidate["family"] != candidate_family:
        raise ValueError("candidate manifest declares another family")
    if candidate["lineage"]["overture_release"] != base["overture_release"]:
        raise ValueError("candidate Overture release differs from the base release")
    v2._validate_family_source(
        candidate_family,
        candidate_source,
        candidate_source_sha256,
        candidate,
        base["overture_release"],
    )
    contract = v2.WORKER_CONSTRUCTION_CONTRACTS.get(
        (candidate_family, candidate["versions"]["format"])
    )
    if contract is None or "forward" not in contract["operations"]:
        raise ValueError("candidate family format has no admitted forward operation")

    family_manifests = dict(base_family_manifests)
    family_sources = dict(base_family_sources)
    family_manifests[candidate_family] = (
        candidate_manifest,
        candidate_manifest_sha256,
    )
    family_sources[candidate_family] = (candidate_source, candidate_source_sha256)

    operations: dict[str, list[str]] = {candidate_family: ["forward"]}
    entrypoints: dict[str, dict[str, str]] = {
        candidate_family: {"forward": f"families/{candidate_family}/{contract['entrypoint']}"}
    }
    omitted_external: dict[str, list[str]] = {}
    for family in sorted(expected_retained):
        reference = base["families"][family]
        external = set((reference.get("operation_sources") or {}).keys())
        retained_operations = [
            operation for operation in reference["operations"] if operation not in external
        ]
        if not retained_operations:
            raise ValueError(f"{family} has no in-source operation to retain")
        operations[family] = retained_operations
        entrypoints[family] = {
            operation: _relative_entrypoint(reference, operation)
            for operation in retained_operations
        }
        if external:
            omitted_external[family] = sorted(external)

    release = v2.build_release_manifest(
        geocoder_build=geocoder_build,
        overture_release=base["overture_release"],
        legacy_release=legacy_release,
        legacy_manifest_sha256=legacy_sha256,
        family_manifests=family_manifests,
        family_source_manifests=family_sources,
        family_operations=operations,
        family_entrypoints=entrypoints,
        generated_at=f"{geocoder_build[:10]}T00:00:00+00:00",
    )
    v2.verify_release_sources(
        release,
        legacy_release=legacy_release,
        legacy_manifest_sha256=legacy_sha256,
        family_manifests=family_manifests,
        family_source_manifests=family_sources,
    )
    report = {
        "schema": "v2-preview-overlay-report-v1",
        "base_build": base["geocoder_build"],
        "preview_build": geocoder_build,
        "candidate_family": candidate_family,
        "candidate_source_version": candidate_source.get("slice_version"),
        "retained_families": sorted(expected_retained),
        "operations": release["operations"],
        "omitted_external_operations": omitted_external,
        "release_digest": release["release_digest"],
    }
    return release, report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-release", type=Path, required=True)
    parser.add_argument("--legacy-release-manifest", type=Path, required=True)
    parser.add_argument("--base-family-manifest", action="append", default=[])
    parser.add_argument("--base-family-source-manifest", action="append", default=[])
    parser.add_argument("--candidate-family", choices=sorted(gbm.FAMILIES), required=True)
    parser.add_argument("--candidate-family-manifest", type=Path, required=True)
    parser.add_argument("--candidate-family-source-manifest", type=Path, required=True)
    parser.add_argument("--geocoder-build", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)

    manifest_paths = parse_family_paths(
        args.base_family_manifest, "--base-family-manifest"
    )
    source_paths = parse_family_paths(
        args.base_family_source_manifest, "--base-family-source-manifest"
    )
    manifests = {
        family: (read_document(path, f"{family} family manifest"), sha256_file(path))
        for family, path in manifest_paths.items()
    }
    sources = {
        family: (read_document(path, f"{family} source manifest"), sha256_file(path))
        for family, path in source_paths.items()
    }
    candidate_manifest_path = args.candidate_family_manifest
    candidate_source_path = args.candidate_family_source_manifest
    release, report = build_overlay_release(
        base_release=read_document(args.base_release, "base release"),
        legacy_release=read_document(
            args.legacy_release_manifest, "legacy release manifest"
        ),
        legacy_sha256=sha256_file(args.legacy_release_manifest),
        base_family_manifests=manifests,
        base_family_sources=sources,
        candidate_family=args.candidate_family,
        candidate_manifest=read_document(
            candidate_manifest_path, "candidate family manifest"
        ),
        candidate_manifest_sha256=sha256_file(candidate_manifest_path),
        candidate_source=read_document(
            candidate_source_path, "candidate family source manifest"
        ),
        candidate_source_sha256=sha256_file(candidate_source_path),
        geocoder_build=args.geocoder_build,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(gbm.canonical_json(release))
    args.report.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
