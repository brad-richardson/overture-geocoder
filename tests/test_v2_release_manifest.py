from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "v2_release_manifest.py"
SPEC = importlib.util.spec_from_file_location("v2_release_manifest", SCRIPT)
assert SPEC and SPEC.loader
v2 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(v2)
gbm = v2.gbm

RELEASE = "2026-06-17.0"


def payload_sha(value) -> str:
    return hashlib.sha256(gbm.canonical_json(value)).hexdigest()


def legacy_release(version: str = "2026-07-18.0", release: str = RELEASE) -> dict:
    return {
        "schema_version": 1,
        "version": version,
        "overture_release": release,
        "families": {"forward": {}, "reverse": {}, "id": {}},
    }


def family_manifest(family: str, release: str = RELEASE) -> dict:
    if family == "places":
        versions = {
            "format": gbm.PLACES_FORMAT_VERSION,
            "tokenizer": gbm.PLACES_TOKENIZER_VERSION,
            "normalization": None,
        }
        scope = "exact"
    else:
        versions = {
            "format": gbm.ADDRESS_FORMAT_VERSION,
            "tokenizer": None,
            "normalization": gbm.ADDRESS_NORMALIZATION_VERSION,
        }
        scope = "row_group_approximate"
    entrypoint = (
        f"families/{family}/catalog.pcat"
        if family == "places"
        else f"families/{family}/address-collection.json"
    )
    return gbm.build_family_manifest(
        family,
        lineage={
            "overture_release": release,
            "build_id": ("a" if family == "places" else "b") * 64,
            "producer_commit": "deadbeef",
            "producer_script": f"scripts/build_{family}.py",
            "producer_version": "1",
        },
        versions=versions,
        region={
            "name": "us-northeast",
            "bbox": [-80.5, 38.0, -66.9, 47.5],
            "bbox_scope": scope,
        },
        artifacts=[
            {
                "object_key": entrypoint,
                "bytes": 123,
                "sha256": "c" * 64,
            }
        ],
        generated_at="2026-07-19T12:00:00+00:00",
    )


def release_manifest(build: str = "2026-07-19.1") -> dict:
    legacy = legacy_release()
    places = family_manifest("places")
    addresses = family_manifest("addresses")
    return v2.build_release_manifest(
        geocoder_build=build,
        overture_release=RELEASE,
        legacy_release=legacy,
        legacy_manifest_sha256=payload_sha(legacy),
        family_manifests={
            "places": (places, payload_sha(places)),
            "addresses": (addresses, payload_sha(addresses)),
        },
        family_entrypoints={
            "places": {"forward": "families/places/catalog.pcat"},
            "addresses": {
                "structured_forward": (
                    "families/addresses/address-collection.json"
                )
            },
        },
        generated_at="2026-07-19T12:00:00+00:00",
    )


def test_release_binds_core_and_family_capabilities():
    manifest = release_manifest()

    assert v2.validate_release_manifest(manifest) == manifest
    assert manifest["data_version"] == {
        "overture_release": RELEASE,
        "geocoder_build": "2026-07-19.1",
    }
    assert manifest["legacy_core"]["manifest_key"] == (
        "2026-07-18.0/release-manifest.json"
    )
    assert manifest["operations"] == {
        "feature_lookup": ["id"],
        "forward": ["divisions", "places"],
        "reverse": ["divisions"],
        "structured_forward": ["addresses"],
    }
    assert manifest["families"]["places"]["coverage"]["name"] == "us-northeast"
    assert manifest["families"]["places"]["entrypoints"]["forward"] == {
        "object_key": "families/places/catalog.pcat",
        "bytes": 123,
        "sha256": "c" * 64,
    }


def test_release_can_explicitly_enable_future_family_operations():
    legacy = legacy_release()
    addresses = family_manifest("addresses")
    manifest = v2.build_release_manifest(
        geocoder_build="2026-07-19.2",
        overture_release=RELEASE,
        legacy_release=legacy,
        legacy_manifest_sha256=payload_sha(legacy),
        family_manifests={"addresses": (addresses, payload_sha(addresses))},
        family_operations={"addresses": ["reverse", "forward"]},
        family_entrypoints={
            "addresses": {
                "forward": "families/addresses/address-collection.json",
                "reverse": "families/addresses/address-collection.json",
            }
        },
        generated_at="now",
    )

    assert manifest["operations"]["forward"] == ["addresses", "divisions"]
    assert manifest["operations"]["reverse"] == ["addresses", "divisions"]
    assert "structured_forward" not in manifest["operations"]


def test_release_rejects_cross_release_family_and_core():
    legacy = legacy_release()
    wrong_family = family_manifest("places", "2026-05-20.0")
    with pytest.raises(ValueError, match="family Overture release differs"):
        v2.build_release_manifest(
            geocoder_build="2026-07-19.1",
            overture_release=RELEASE,
            legacy_release=legacy,
            legacy_manifest_sha256=payload_sha(legacy),
            family_manifests={"places": (wrong_family, payload_sha(wrong_family))},
        )

    with pytest.raises(ValueError, match="legacy release Overture release differs"):
        v2.build_release_manifest(
            geocoder_build="2026-07-19.1",
            overture_release=RELEASE,
            legacy_release=legacy_release(release="2026-05-20.0"),
            legacy_manifest_sha256="d" * 64,
        )


@pytest.mark.parametrize("version", ["../catalog", "nested/version", r"nested\version"])
def test_release_builder_rejects_unsafe_legacy_version(version):
    legacy = legacy_release(version=version)
    with pytest.raises(ValueError, match="canonical object-key component"):
        v2.build_release_manifest(
            geocoder_build="2026-07-19.1",
            overture_release=RELEASE,
            legacy_release=legacy,
            legacy_manifest_sha256=payload_sha(legacy),
        )


@pytest.mark.parametrize("build", ["latest", "2026-07-19", "../2026-07-19.1"])
def test_release_rejects_unsafe_or_nonmonotonic_build_identity(build):
    legacy = legacy_release()
    with pytest.raises(ValueError, match="YYYY-MM-DD.N"):
        v2.build_release_manifest(
            geocoder_build=build,
            overture_release=RELEASE,
            legacy_release=legacy,
            legacy_manifest_sha256=payload_sha(legacy),
        )


def test_release_rejects_unsupported_or_orphan_operations():
    legacy = legacy_release()
    places = family_manifest("places")
    with pytest.raises(ValueError, match="unsupported places operations"):
        v2.build_release_manifest(
            geocoder_build="2026-07-19.1",
            overture_release=RELEASE,
            legacy_release=legacy,
            legacy_manifest_sha256=payload_sha(legacy),
            family_manifests={"places": (places, payload_sha(places))},
            family_operations={"places": ["structured_forward"]},
            family_entrypoints={
                "places": {
                    "structured_forward": "families/places/catalog.pcat"
                }
            },
        )
    with pytest.raises(ValueError, match="absent families"):
        v2.build_release_manifest(
            geocoder_build="2026-07-19.1",
            overture_release=RELEASE,
            legacy_release=legacy,
            legacy_manifest_sha256=payload_sha(legacy),
            family_operations={"places": ["forward"]},
        )


def test_release_requires_verified_entrypoint_for_every_operation():
    legacy = legacy_release()
    places = family_manifest("places")
    with pytest.raises(ValueError, match="entrypoints must name exactly"):
        v2.build_release_manifest(
            geocoder_build="2026-07-19.1",
            overture_release=RELEASE,
            legacy_release=legacy,
            legacy_manifest_sha256=payload_sha(legacy),
            family_manifests={"places": (places, payload_sha(places))},
        )
    with pytest.raises(ValueError, match="not a manifest artifact"):
        v2.build_release_manifest(
            geocoder_build="2026-07-19.1",
            overture_release=RELEASE,
            legacy_release=legacy,
            legacy_manifest_sha256=payload_sha(legacy),
            family_manifests={"places": (places, payload_sha(places))},
            family_entrypoints={
                "places": {"forward": "families/places/missing.pcat"}
            },
        )


def test_release_validation_detects_capability_and_digest_tampering():
    manifest = release_manifest()
    tampered = copy.deepcopy(manifest)
    tampered["operations"]["forward"] = ["divisions"]
    with pytest.raises(ValueError, match="operations differ"):
        v2.validate_release_manifest(tampered)

    tampered = copy.deepcopy(manifest)
    tampered["families"]["places"]["coverage"]["name"] = "elsewhere"
    with pytest.raises(ValueError, match="release_digest"):
        v2.validate_release_manifest(tampered)

    tampered = copy.deepcopy(manifest)
    tampered["families"]["places"]["entrypoints"]["forward"]["object_key"] = (
        "families/addresses/address-collection.json"
    )
    unsigned = {key: value for key, value in tampered.items() if key != "release_digest"}
    tampered["release_digest"] = gbm.digest(unsigned)
    with pytest.raises(ValueError, match="must remain under families/places"):
        v2.validate_release_manifest(tampered)


def test_source_verification_rejects_recomputed_unlisted_entrypoint():
    manifest = release_manifest()
    legacy = legacy_release()
    places = family_manifest("places")
    addresses = family_manifest("addresses")
    tampered = copy.deepcopy(manifest)
    tampered["families"]["places"]["entrypoints"]["forward"] = {
        "object_key": "families/places/unlisted.pcat",
        "bytes": 999,
        "sha256": "d" * 64,
    }
    unsigned = {key: value for key, value in tampered.items() if key != "release_digest"}
    tampered["release_digest"] = gbm.digest(unsigned)

    # Its self-contained shape and recomputed digest are valid, but the strong
    # source boundary refuses to bless an entrypoint absent from the family.
    assert v2.validate_release_manifest(tampered) == tampered
    with pytest.raises(ValueError, match="differs from its source artifact"):
        v2.verify_release_sources(
            tampered,
            legacy_release=legacy,
            legacy_manifest_sha256=payload_sha(legacy),
            family_manifests={
                "places": (places, payload_sha(places)),
                "addresses": (addresses, payload_sha(addresses)),
            },
        )


def test_catalog_is_monotonic_and_preserves_history():
    first_release = release_manifest("2026-07-19.1")
    first = v2.build_catalog(
        release_manifest=first_release,
        release_manifest_sha256=payload_sha(first_release),
        initialize=True,
        generated_at="first",
    )
    second_release = release_manifest("2026-07-19.2")
    second = v2.build_catalog(
        release_manifest=second_release,
        release_manifest_sha256=payload_sha(second_release),
        before=first,
        generated_at="second",
    )

    assert v2.validate_catalog(second) == second
    assert second["latest"] == "2026-07-19.2"
    assert [entry["geocoder_build"] for entry in second["releases"]] == [
        "2026-07-19.2",
        "2026-07-19.1",
    ]
    assert second["releases"][0]["manifest_key"] == (
        "v2/releases/2026-07-19.2/release.json"
    )


def test_catalog_rejects_duplicate_or_rollback_build():
    current_release = release_manifest("2026-07-19.2")
    current = v2.build_catalog(
        release_manifest=current_release,
        release_manifest_sha256=payload_sha(current_release),
        initialize=True,
    )
    for build in ("2026-07-19.2", "2026-07-19.1"):
        candidate = release_manifest(build)
        with pytest.raises(ValueError, match="must be newer"):
            v2.build_catalog(
                release_manifest=candidate,
                release_manifest_sha256=payload_sha(candidate),
                before=current,
            )


def test_catalog_validation_detects_key_and_digest_tampering():
    release = release_manifest()
    catalog = v2.build_catalog(
        release_manifest=release,
        release_manifest_sha256=payload_sha(release),
        initialize=True,
    )
    bad_key = copy.deepcopy(catalog)
    bad_key["releases"][0]["manifest_key"] = "v2/releases/../catalog.json"
    with pytest.raises(ValueError, match="canonical relative object key"):
        v2.validate_catalog(bad_key)

    bad_digest = copy.deepcopy(catalog)
    bad_digest["generated_at"] = "changed"
    with pytest.raises(ValueError, match="catalog_digest"):
        v2.validate_catalog(bad_digest)


def test_catalog_requires_explicit_initialization_or_history():
    release = release_manifest()
    with pytest.raises(ValueError, match="exactly one"):
        v2.build_catalog(
            release_manifest=release,
            release_manifest_sha256=payload_sha(release),
        )
    with pytest.raises(ValueError, match="exactly one"):
        v2.build_catalog(
            release_manifest=release,
            release_manifest_sha256=payload_sha(release),
            before={"not": "used"},
            initialize=True,
        )


def test_cli_builds_and_validates_release_and_catalog(tmp_path):
    legacy = legacy_release()
    places = family_manifest("places")
    legacy_path = tmp_path / "legacy.json"
    places_path = tmp_path / "places.json"
    legacy_path.write_text(json.dumps(legacy))
    places_path.write_bytes(gbm.canonical_json(places))
    release_path = tmp_path / "release.json"
    catalog_path = tmp_path / "catalog.json"

    built = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "release",
            "--geocoder-build",
            "2026-07-19.1",
            "--overture-release",
            RELEASE,
            "--legacy-release-manifest",
            str(legacy_path),
            "--family-manifest",
            f"places={places_path}",
            "--entrypoint",
            "places.forward=families/places/catalog.pcat",
            "--generated-at",
            "fixed",
            "--output",
            str(release_path),
        ],
        text=True,
        capture_output=True,
    )
    assert built.returncode == 0, built.stderr
    checked = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "validate-release",
            "--manifest",
            str(release_path),
            "--legacy-release-manifest",
            str(legacy_path),
            "--family-manifest",
            f"places={places_path}",
        ],
        text=True,
        capture_output=True,
    )
    assert checked.returncode == 0, checked.stderr

    built_catalog = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "catalog",
            "--release-manifest",
            str(release_path),
            "--initialize",
            "--generated-at",
            "fixed",
            "--output",
            str(catalog_path),
        ],
        text=True,
        capture_output=True,
    )
    assert built_catalog.returncode == 0, built_catalog.stderr
    assert v2.validate_catalog(json.loads(catalog_path.read_text()))["latest"] == (
        "2026-07-19.1"
    )
