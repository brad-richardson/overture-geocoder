from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "global_build_manifest.py"
SPEC = importlib.util.spec_from_file_location("global_build_manifest", SCRIPT)
assert SPEC and SPEC.loader
manifest = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manifest)


def _artifacts(tmp_path: Path) -> list[tuple[str, Path]]:
    specs = []
    for index in range(3):
        path = tmp_path / f"us-northeast-{index:04d}.pcsh"
        path.write_bytes(f"shard-body-{index}".encode() * 8)
        key = f"smoke/places-region/run/sha256/{'0' * 64}/{path.name}"
        specs.append((key, path))
    return specs


def test_artifact_from_spec_recomputes_identity(tmp_path):
    path = tmp_path / "shard.pcsh"
    path.write_bytes(b"hello world")
    entry = manifest.artifact_from_spec(f"the/object/key.pcsh={path}")
    assert entry["object_key"] == "the/object/key.pcsh"
    assert entry["bytes"] == 11
    assert entry["sha256"] == hashlib.sha256(b"hello world").hexdigest()


def test_artifact_from_spec_rejects_malformed_and_empty(tmp_path):
    with pytest.raises(ValueError):
        manifest.artifact_from_spec("no-equals-sign")
    empty = tmp_path / "empty.pcsh"
    empty.write_bytes(b"")
    with pytest.raises(ValueError):
        manifest.artifact_from_spec(f"k={empty}")


def test_derive_direct_build_id_is_deterministic_hex():
    region = {"name": "ne", "bbox": [-80.5, 38.0, -66.9, 47.5], "bbox_scope": "exact"}
    artifacts = [{"object_key": "b"}, {"object_key": "a"}]
    first = manifest.derive_direct_build_id(
        family="places", release="2026-06-17.0", producer_commit="abc",
        region=region, artifacts=artifacts,
    )
    # Order-independent (keys are sorted) and a canonical sha-256.
    second = manifest.derive_direct_build_id(
        family="places", release="2026-06-17.0", producer_commit="abc",
        region=region, artifacts=list(reversed(artifacts)),
    )
    assert first == second
    manifest.require_hex_digest(first, "build_id")


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        text=True, capture_output=True,
    )


def test_family_manifest_build_and_verify_round_trip(tmp_path):
    specs = _artifacts(tmp_path)
    artifact_flags: list[str] = []
    for key, path in specs:
        artifact_flags += ["--artifact", f"{key}={path}"]
    out = tmp_path / "family-manifest.json"
    built = _run(
        [
            "family-manifest", "--family", "places",
            "--overture-release", "2026-06-17.0", "--producer-commit", "deadbeef",
            "--producer-script", "scripts/build_places_region_shards.py",
            "--producer-version", "1", "--region-name", "us-northeast",
            "--bbox", "-80.5", "38.0", "-66.9", "47.5", "--bbox-scope", "exact",
            *artifact_flags, "--output", str(out),
        ]
    )
    assert built.returncode == 0, built.stderr
    payload = json.loads(out.read_text())
    assert payload["schema"] == manifest.FAMILY_MANIFEST_SCHEMA
    assert payload["family"] == "places"
    assert payload["versions"]["format"] == manifest.PLACES_FORMAT_VERSION
    assert payload["versions"]["tokenizer"] == manifest.PLACES_TOKENIZER_VERSION
    assert payload["region"]["bbox_scope"] == "exact"
    assert payload["totals"]["artifacts"] == 3
    # The manifest self-digest is internally consistent.
    manifest.validate_family_manifest(payload)

    # A listing exactly matching the published identities verifies.
    listing = {
        entry["object_key"]: [entry["bytes"], entry["sha256"]]
        for entry in payload["artifacts"]
    }
    listing_path = tmp_path / "listing.json"
    listing_path.write_text(json.dumps(listing))
    ok = _run(
        ["verify-family-manifest", "--manifest", str(out), "--listing", str(listing_path)]
    )
    assert ok.returncode == 0, ok.stderr
    assert json.loads(ok.stdout)["verified_objects"] == 3

    # A tampered listing (wrong size) fails verification.
    key = next(iter(listing))
    listing[key] = [listing[key][0] + 1, listing[key][1]]
    bad_path = tmp_path / "listing-bad.json"
    bad_path.write_text(json.dumps(listing))
    bad = _run(
        ["verify-family-manifest", "--manifest", str(out), "--listing", str(bad_path)]
    )
    assert bad.returncode != 0


def test_family_manifest_defaults_addresses_normalization(tmp_path):
    specs = _artifacts(tmp_path)
    artifact_flags: list[str] = []
    for key, path in specs:
        artifact_flags += ["--artifact", f"{key}={path}"]
    out = tmp_path / "addresses-manifest.json"
    built = _run(
        [
            "family-manifest", "--family", "addresses",
            "--overture-release", "2026-06-17.0", "--producer-commit", "deadbeef",
            "--producer-script", "scripts/experiment_hosted_rowgroups.py",
            "--producer-version", "1", "--region-name", "us-northeast",
            "--bbox", "-80.5", "38.0", "-66.9", "47.5",
            "--bbox-scope", "row_group_approximate",
            *artifact_flags, "--output", str(out),
        ]
    )
    assert built.returncode == 0, built.stderr
    payload = json.loads(out.read_text())
    assert payload["versions"]["format"] == manifest.ADDRESS_FORMAT_VERSION
    assert payload["versions"]["normalization"] == manifest.ADDRESS_NORMALIZATION_VERSION
    assert payload["versions"]["tokenizer"] is None
