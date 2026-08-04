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
SLICE = "slice-2026-07-19.0"


def payload_sha(value) -> str:
    return hashlib.sha256(gbm.canonical_json(value)).hexdigest()


def legacy_release(version: str = "2026-07-18.0", release: str = RELEASE) -> dict:
    forward = {"href": "./shards/AA.db", "size_bytes": 11, "sha256": "a" * 64}
    reverse = {"href": "./reverse/AA.db", "size_bytes": 12, "sha256": "b" * 64}
    router = {"href": "./router.db", "size_bytes": 13, "sha256": "c" * 64}
    dictionary = {
        "href": f"./id-locator-dictionary-{'d' * 64}.json",
        "size_bytes": 14,
        "sha256": "d" * 64,
    }
    identifiers = [
        {
            "href": f"./id-index/{prefix:03x}.parquet",
            "size_bytes": 1,
            "sha256": "e" * 64,
        }
        for prefix in range(16**3)
    ]
    verified = [
        {"href": "./collection.json", "size_bytes": 21},
        {"href": "./reverse-collection.json", "size_bytes": 22},
        {"href": "./id-collection.json", "size_bytes": 23},
        {"href": forward["href"], "size_bytes": forward["size_bytes"]},
        {"href": reverse["href"], "size_bytes": reverse["size_bytes"]},
        {"href": router["href"], "size_bytes": router["size_bytes"]},
        {"href": dictionary["href"], "size_bytes": dictionary["size_bytes"]},
        *[
            {"href": item["href"], "size_bytes": item["size_bytes"]}
            for item in identifiers
        ],
    ]
    return {
        "schema_version": 1,
        "version": version,
        "overture_release": release,
        "generated_at": "2026-07-19T12:00:00+00:00",
        "families": {
            "forward": {
                "collection": "./collection.json",
                "shard_count": 1,
                "objects": [forward],
                "router": router,
            },
            "reverse": {
                "collection": "./reverse-collection.json",
                "shard_count": 1,
                "objects": [reverse],
            },
            "id": {
                "collection": "./id-collection.json",
                "format_version": 3,
                "shard_count": 4096,
                "total_size_bytes": 4096,
                "objects": identifiers,
                "integrity": "fixture",
                "locator_dictionary": dictionary,
            },
        },
        "verified_version_objects": verified,
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
    artifacts = [
        {
            "object_key": entrypoint,
            "bytes": 123,
            "sha256": "c" * 64,
        }
    ]
    if family == "places":
        artifacts.append(
            {
                "object_key": "families/places/head.phrp",
                "bytes": 45,
                "sha256": "f" * 64,
            }
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
        artifacts=artifacts,
        generated_at="2026-07-19T12:00:00+00:00",
    )


def family_source_manifest(
    *families: str, release: str = RELEASE, version: str = SLICE
) -> dict:
    manifests = {family: family_manifest(family, release) for family in families}
    summaries = {}
    verified_objects = []
    for family, manifest in manifests.items():
        artifacts = manifest["artifacts"]
        manifest_href = f"./families/{family}/family-manifest.json"
        summaries[family] = {
            "manifest": manifest_href,
            "manifest_digest": manifest["manifest_digest"],
            "region": manifest["region"],
            "artifact_count": len(artifacts),
            "total_bytes": sum(artifact["bytes"] for artifact in artifacts),
            "objects": [
                {
                    "href": f"./{artifact['object_key']}",
                    "size_bytes": artifact["bytes"],
                    "sha256": artifact["sha256"],
                }
                for artifact in artifacts
            ],
            "promotion_eligible": False,
        }
        verified_objects.append({"href": manifest_href})
        verified_objects.extend(summaries[family]["objects"])
    return {
        "schema_version": 1,
        "slice_version": version,
        "overture_release": release,
        "generated_at": "2026-07-19T12:00:00+00:00",
        "is_slice": True,
        "promotion_eligible": False,
        "families": summaries,
        "verified_version_objects": verified_objects,
    }


def family_source_args(family: str, release: str = RELEASE) -> dict:
    source = family_source_manifest(family, release=release)
    return {
        "family_source_manifests": {
            family: (source, payload_sha(source)),
        }
    }


def release_manifest(build: str = "2026-07-19.1") -> dict:
    legacy = legacy_release()
    places = family_manifest("places")
    addresses = family_manifest("addresses")
    source = family_source_manifest("places", "addresses")
    return v2.build_release_manifest(
        geocoder_build=build,
        overture_release=RELEASE,
        legacy_release=legacy,
        legacy_manifest_sha256=payload_sha(legacy),
        family_manifests={
            "places": (places, payload_sha(places)),
            "addresses": (addresses, payload_sha(addresses)),
        },
        family_source_manifests={
            "places": (source, payload_sha(source)),
            "addresses": (source, payload_sha(source)),
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


def external_reverse(family: str, version: str, request: str, sha: str) -> dict:
    claim_payload = gbm.canonical_json(
        {
            "schema": v2.SLICE_CLAIM_SCHEMA,
            "version": version,
            "family": family,
            "request_sha256": request,
            "overture_release": RELEASE,
        }
    )
    return {
        "source": {
            "kind": v2.EXTERNAL_OPERATION_KIND,
            "version": version,
            "request_sha256": request,
            "slice_claim": {
                "object_key": f"{version}/claims/{family}.json",
                "bytes": len(claim_payload),
                "sha256": hashlib.sha256(claim_payload).hexdigest(),
            },
        },
        "entrypoint": {
            "object_key": f"{version}/families/{family}/reverse-catalog.rcat",
            "bytes": v2.REVERSE_ROOT_BYTES,
            "sha256": sha,
        },
    }


def test_overlay_replaces_candidate_and_preserves_all_retained_operations():
    legacy = legacy_release()
    places = family_manifest("places")
    addresses = family_manifest("addresses")
    base_source = family_source_manifest("places", "addresses")
    address_reverse = external_reverse(
        "addresses", "slice-2026-07-18.0", "b" * 64, "8" * 64
    )
    base_places_reverse = external_reverse(
        "places", "slice-2026-07-17.0", "a" * 64, "7" * 64
    )
    base = v2.build_release_manifest(
        geocoder_build="2026-07-19.1",
        overture_release=RELEASE,
        legacy_release=legacy,
        legacy_manifest_sha256=payload_sha(legacy),
        family_manifests={
            "places": (places, payload_sha(places)),
            "addresses": (addresses, payload_sha(addresses)),
        },
        family_source_manifests={
            "places": (base_source, payload_sha(base_source)),
            "addresses": (base_source, payload_sha(base_source)),
        },
        family_operations={
            "places": ["forward", "reverse"],
            "addresses": ["reverse", "structured_forward"],
        },
        family_entrypoints={
            "places": {
                "forward": "families/places/catalog.pcat",
                "reverse": base_places_reverse["entrypoint"]["object_key"],
            },
            "addresses": {
                "structured_forward": "families/addresses/address-collection.json",
                "reverse": address_reverse["entrypoint"]["object_key"],
            },
        },
        family_external_operations={
            "places": {"reverse": base_places_reverse},
            "addresses": {"reverse": address_reverse},
        },
        generated_at="2026-07-19T12:00:00+00:00",
    )
    candidate = gbm.build_family_manifest(
        "places",
        lineage={
            "overture_release": RELEASE,
            "build_id": "d" * 64,
            "producer_commit": "deadbeef",
            "producer_script": "scripts/places_construction_v1.py",
            "producer_version": "construction-v1",
        },
        versions={
            "format": "PLRV0003+PLHD0003",
            "tokenizer": "nfkd-lower-stripmark-cjk-bigram-v4",
            "normalization": None,
        },
        region={
            "name": "global",
            "bbox": [-180.0, -90.0, 180.0, 90.0],
            "bbox_scope": "exact",
        },
        artifacts=[
            {
                "object_key": "families/places/routing.json",
                "bytes": 123,
                "sha256": "c" * 64,
            }
        ],
        generated_at="2026-07-20T00:00:00+00:00",
    )
    candidate_source = family_source_manifest(
        "places", version="slice-2026-07-20.0"
    )
    candidate_source["families"]["places"] = {
        "manifest": "./families/places/family-manifest.json",
        "manifest_digest": candidate["manifest_digest"],
        "region": candidate["region"],
        "artifact_count": 1,
        "total_bytes": 123,
        "objects": [
            {
                "href": "./families/places/routing.json",
                "size_bytes": 123,
                "sha256": "c" * 64,
            }
        ],
        "promotion_eligible": False,
    }
    candidate_source["verified_version_objects"] = [
        {"href": "./families/places/family-manifest.json"},
        {
            "href": "./families/places/routing.json",
            "size_bytes": 123,
            "sha256": "c" * 64,
        },
    ]
    places_reverse = external_reverse(
        "places", "slice-2026-07-21.0", "d" * 64, "9" * 64
    )
    base_sources = {
        "legacy_release": legacy,
        "legacy_manifest_sha256": payload_sha(legacy),
        "family_manifests": {
            "places": (places, payload_sha(places)),
            "addresses": (addresses, payload_sha(addresses)),
        },
        "family_source_manifests": {
            "places": (base_source, payload_sha(base_source)),
            "addresses": (base_source, payload_sha(base_source)),
        },
    }

    result = v2.build_overlay_release_manifest(
        base_release=base,
        geocoder_build="2026-07-22.0",
        base_sources=base_sources,
        candidate_family="places",
        candidate_manifest=candidate,
        candidate_manifest_sha256=payload_sha(candidate),
        candidate_source_manifest=candidate_source,
        candidate_source_sha256=payload_sha(candidate_source),
        candidate_external_operations={"reverse": places_reverse},
    )

    assert result["families"]["addresses"] == base["families"]["addresses"]
    assert result["families"]["places"]["source"]["version"] == (
        "slice-2026-07-20.0"
    )
    assert result["families"]["places"]["operations"] == ["forward", "reverse"]
    assert result["families"]["places"]["operation_sources"]["reverse"] == (
        places_reverse["source"]
    )
    assert result["operations"] == base["operations"]


def catalog_sources() -> dict:
    legacy = legacy_release()
    places = family_manifest("places")
    addresses = family_manifest("addresses")
    source = family_source_manifest("places", "addresses")
    return {
        "legacy_release": legacy,
        "legacy_manifest_sha256": payload_sha(legacy),
        "family_manifests": {
            "places": (places, payload_sha(places)),
            "addresses": (addresses, payload_sha(addresses)),
        },
        "family_source_manifests": {
            "places": (source, payload_sha(source)),
            "addresses": (source, payload_sha(source)),
        },
    }


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
        "object_key": "slice-2026-07-19.0/families/places/catalog.pcat",
        "bytes": 123,
        "sha256": "c" * 64,
    }
    assert manifest["families"]["places"]["source"] == {
        "kind": "family_slice",
        "version": SLICE,
        "manifest_key": f"{SLICE}/slice-manifest.json",
        "manifest_sha256": payload_sha(
            family_source_manifest("places", "addresses")
        ),
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
        **family_source_args("addresses"),
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


def test_places_forward_requires_hashed_global_head_artifact():
    legacy = legacy_release()
    places = family_manifest("places")
    places["artifacts"] = [
        artifact
        for artifact in places["artifacts"]
        if artifact["object_key"] != "families/places/head.phrp"
    ]
    places["totals"] = {
        "artifacts": len(places["artifacts"]),
        "bytes": sum(artifact["bytes"] for artifact in places["artifacts"]),
    }
    unsigned = {key: value for key, value in places.items() if key != "manifest_digest"}
    places["manifest_digest"] = gbm.digest(unsigned)
    source = family_source_manifest("places")
    source["families"]["places"]["manifest_digest"] = places["manifest_digest"]
    source["families"]["places"]["artifact_count"] = len(places["artifacts"])
    source["families"]["places"]["total_bytes"] = sum(
        artifact["bytes"] for artifact in places["artifacts"]
    )
    source["families"]["places"]["objects"] = [
        {
            "href": f"./{artifact['object_key']}",
            "size_bytes": artifact["bytes"],
            "sha256": artifact["sha256"],
        }
        for artifact in places["artifacts"]
    ]
    source["verified_version_objects"] = [
        {"href": "./families/places/family-manifest.json"},
        *source["families"]["places"]["objects"],
    ]

    with pytest.raises(
        ValueError,
        match="places forward requires manifest artifact families/places/head.phrp",
    ):
        v2.build_release_manifest(
            geocoder_build="2026-07-19.2",
            overture_release=RELEASE,
            legacy_release=legacy,
            legacy_manifest_sha256=payload_sha(legacy),
            family_manifests={"places": (places, payload_sha(places))},
            family_source_manifests={"places": (source, payload_sha(source))},
            family_entrypoints={
                "places": {"forward": "families/places/catalog.pcat"}
            },
            generated_at="now",
        )


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
            **family_source_args("places", release="2026-05-20.0"),
        )

    with pytest.raises(ValueError, match="legacy release Overture release differs"):
        v2.build_release_manifest(
            geocoder_build="2026-07-19.1",
            overture_release=RELEASE,
            legacy_release=legacy_release(release="2026-05-20.0"),
            legacy_manifest_sha256="d" * 64,
        )


def test_release_rejects_stubbed_legacy_core_manifest():
    stub = {
        "schema_version": 1,
        "version": "2026-07-18.0",
        "overture_release": RELEASE,
        "generated_at": "now",
        "families": {"forward": {}, "reverse": {}, "id": {}},
        "verified_version_objects": [],
    }
    with pytest.raises(ValueError, match="forward collection path differs"):
        v2.build_release_manifest(
            geocoder_build="2026-07-19.1",
            overture_release=RELEASE,
            legacy_release=stub,
            legacy_manifest_sha256=payload_sha(stub),
        )


def test_release_rejects_locator_dictionary_aliasing_a_core_object():
    legacy = legacy_release()
    legacy["families"]["id"]["locator_dictionary"] = copy.deepcopy(
        legacy["families"]["id"]["objects"][0]
    )
    with pytest.raises(ValueError, match="dictionary path differs"):
        v2.build_release_manifest(
            geocoder_build="2026-07-19.1",
            overture_release=RELEASE,
            legacy_release=legacy,
            legacy_manifest_sha256=payload_sha(legacy),
        )


def test_release_rejects_family_not_blessed_by_its_source_manifest():
    legacy = legacy_release()
    places = family_manifest("places")
    with pytest.raises(ValueError, match="not verified by its source manifest"):
        v2.build_release_manifest(
            geocoder_build="2026-07-19.1",
            overture_release=RELEASE,
            legacy_release=legacy,
            legacy_manifest_sha256=payload_sha(legacy),
            family_manifests={"places": (places, payload_sha(places))},
            family_source_manifests={"places": (legacy, payload_sha(legacy))},
            family_entrypoints={
                "places": {"forward": "families/places/catalog.pcat"}
            },
        )


def test_release_rejects_conflicting_proofs_for_one_source_key():
    legacy = legacy_release()
    places = family_manifest("places")
    addresses = family_manifest("addresses")
    places_source = family_source_manifest("places")
    addresses_source = family_source_manifest("addresses")
    with pytest.raises(ValueError, match="conflicting SHA-256"):
        v2.build_release_manifest(
            geocoder_build="2026-07-19.1",
            overture_release=RELEASE,
            legacy_release=legacy,
            legacy_manifest_sha256=payload_sha(legacy),
            family_manifests={
                "places": (places, payload_sha(places)),
                "addresses": (addresses, payload_sha(addresses)),
            },
            family_source_manifests={
                "places": (places_source, payload_sha(places_source)),
                "addresses": (addresses_source, payload_sha(addresses_source)),
            },
            family_entrypoints={
                "places": {"forward": "families/places/catalog.pcat"},
                "addresses": {
                    "structured_forward": (
                        "families/addresses/address-collection.json"
                    )
                },
            },
        )


@pytest.mark.parametrize("version", ["../catalog", "nested/version", r"nested\version"])
def test_release_builder_rejects_unsafe_legacy_version(version):
    legacy = legacy_release(version=version)
    with pytest.raises(ValueError, match="YYYY-MM-DD.N"):
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
            **family_source_args("places"),
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
            **family_source_args("places"),
        )
    with pytest.raises(ValueError, match="not a manifest artifact"):
        v2.build_release_manifest(
            geocoder_build="2026-07-19.1",
            overture_release=RELEASE,
            legacy_release=legacy,
            legacy_manifest_sha256=payload_sha(legacy),
            family_manifests={"places": (places, payload_sha(places))},
            **family_source_args("places"),
            family_entrypoints={
                "places": {"forward": "families/places/missing.pcat"}
            },
        )


def test_release_validation_detects_capability_and_digest_tampering():
    manifest = release_manifest()
    tampered = copy.deepcopy(manifest)
    tampered["families"]["addresses"]["source"]["manifest_sha256"] = "d" * 64
    unsigned = {key: value for key, value in tampered.items() if key != "release_digest"}
    tampered["release_digest"] = gbm.digest(unsigned)
    with pytest.raises(ValueError, match="conflicting SHA-256"):
        v2.validate_release_manifest(tampered)

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
        "slice-2026-07-19.0/families/addresses/address-collection.json"
    )
    unsigned = {key: value for key, value in tampered.items() if key != "release_digest"}
    tampered["release_digest"] = gbm.digest(unsigned)
    with pytest.raises(
        ValueError, match="must remain under slice-2026-07-19.0/families/places"
    ):
        v2.validate_release_manifest(tampered)


def test_source_verification_rejects_recomputed_unlisted_entrypoint():
    manifest = release_manifest()
    legacy = legacy_release()
    places = family_manifest("places")
    addresses = family_manifest("addresses")
    tampered = copy.deepcopy(manifest)
    tampered["families"]["places"]["entrypoints"]["forward"] = {
        "object_key": "slice-2026-07-19.0/families/places/unlisted.pcat",
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
            family_source_manifests=catalog_sources()[
                "family_source_manifests"
            ],
        )

    with pytest.raises(ValueError, match="differs from its source artifact"):
        v2.build_catalog(
            release_manifest=tampered,
            release_manifest_sha256=payload_sha(tampered),
            **catalog_sources(),
            initialize=True,
        )


def test_catalog_is_monotonic_and_preserves_history():
    first_release = release_manifest("2026-07-19.1")
    first = v2.build_catalog(
        release_manifest=first_release,
        release_manifest_sha256=payload_sha(first_release),
        **catalog_sources(),
        initialize=True,
        generated_at="first",
    )
    second_release = release_manifest("2026-07-19.2")
    second = v2.build_catalog(
        release_manifest=second_release,
        release_manifest_sha256=payload_sha(second_release),
        **catalog_sources(),
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
        **catalog_sources(),
        initialize=True,
    )
    for build in ("2026-07-19.2", "2026-07-19.1"):
        candidate = release_manifest(build)
        with pytest.raises(ValueError, match="must be newer"):
            v2.build_catalog(
                release_manifest=candidate,
                release_manifest_sha256=payload_sha(candidate),
                **catalog_sources(),
                before=current,
            )


def test_catalog_validation_detects_key_and_digest_tampering():
    release = release_manifest()
    catalog = v2.build_catalog(
        release_manifest=release,
        release_manifest_sha256=payload_sha(release),
        **catalog_sources(),
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


def test_preview_catalog_is_single_release_and_uses_isolated_sibling_manifest():
    release = release_manifest()
    preview_key = "smoketest-v2/run-29705861699-1/catalog.json"
    catalog = v2.build_catalog(
        release_manifest=release,
        release_manifest_sha256=payload_sha(release),
        **catalog_sources(),
        initialize=True,
        catalog_key=preview_key,
    )

    assert catalog["releases"][0]["manifest_key"] == (
        "smoketest-v2/run-29705861699-1/release.json"
    )
    assert v2.validate_catalog(catalog, catalog_key=preview_key) == catalog
    with pytest.raises(ValueError, match="differs from geocoder_build"):
        v2.validate_catalog(catalog)

    with pytest.raises(ValueError, match="cannot preserve release history"):
        v2.build_catalog(
            release_manifest=release_manifest("2026-07-19.2"),
            release_manifest_sha256=payload_sha(release_manifest("2026-07-19.2")),
            **catalog_sources(),
            before=catalog,
            catalog_key=preview_key,
        )
    for bad_key in (
        "smoketest-v2/catalog.json",
        "smoketest-v2/../catalog.json",
        "smoketest-v2/run.with-dot/catalog.json",
    ):
        with pytest.raises(ValueError, match="v2 catalog key"):
            v2.build_catalog(
                release_manifest=release,
                release_manifest_sha256=payload_sha(release),
                **catalog_sources(),
                initialize=True,
                catalog_key=bad_key,
            )


def test_catalog_requires_explicit_initialization_or_history():
    release = release_manifest()
    with pytest.raises(ValueError, match="exactly one"):
        v2.build_catalog(
            release_manifest=release,
            release_manifest_sha256=payload_sha(release),
            **catalog_sources(),
        )
    with pytest.raises(ValueError, match="exactly one"):
        v2.build_catalog(
            release_manifest=release,
            release_manifest_sha256=payload_sha(release),
            **catalog_sources(),
            before={"not": "used"},
            initialize=True,
        )


def test_cli_builds_and_validates_release_and_catalog(tmp_path):
    legacy = legacy_release()
    places = family_manifest("places")
    source = family_source_manifest("places")
    legacy_path = tmp_path / "legacy.json"
    places_path = tmp_path / "places.json"
    source_path = tmp_path / "slice.json"
    legacy_path.write_text(json.dumps(legacy))
    places_path.write_bytes(gbm.canonical_json(places))
    source_path.write_bytes(gbm.canonical_json(source))
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
            "--family-source-manifest",
            f"places={source_path}",
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
            "--family-source-manifest",
            f"places={source_path}",
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
            "--legacy-release-manifest",
            str(legacy_path),
            "--family-manifest",
            f"places={places_path}",
            "--family-source-manifest",
            f"places={source_path}",
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
