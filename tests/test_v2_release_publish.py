"""Publication-layer tests for v2_release_manifest: assemble / publish-release /
promote / recover over a stubbed R2 client (no credentials, no boto3), plus the
worker-contract parity test for the assembled release document.

The stub client honours the exact conditional surface the CAS layer relies on:
``If-None-Match: *`` on the create-only PUT, ``If-Match: <etag>`` on the
compare-and-swap PUT and on DeleteObject, with a single-part (content-MD5)
ETag -- the R2 semantics the module documents as requiring a one-object live
probe before first production use.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

SPEC = importlib.util.spec_from_file_location(
    "v2_release_manifest", SCRIPTS / "v2_release_manifest.py"
)
assert SPEC and SPEC.loader
v2 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(v2)
gbm = v2.gbm

# The sibling document-layer test module supplies the legacy core fixture.
FIXTURE_SPEC = importlib.util.spec_from_file_location(
    "v2_release_manifest_fixtures", Path(__file__).parent / "test_v2_release_manifest.py"
)
assert FIXTURE_SPEC and FIXTURE_SPEC.loader
fixtures = importlib.util.module_from_spec(FIXTURE_SPEC)
FIXTURE_SPEC.loader.exec_module(fixtures)


RELEASE = "2026-06-17.0"
SLICE = "slice-2026-07-28.0"
LEGACY = "2026-07-18.0"
BUILD = "2026-07-28.0"
NEWER_BUILD = "2026-07-29.0"
CATALOG_KEY = "v2/catalog.json"

# Worker admission contract, mined from crates/geocoder-worker/src/v2.rs and
# the construction modules it imports. Pinned literally here so a producer
# drift shows up as a failed equality, not a moved constant.
PLACES_FORMAT = "PLRV0002+PLHD0002"  # places_construction_v1.rs:32
ADDRESS_FORMAT = "OAV1ART"  # address_construction_v1.rs:47
PLACES_TOKENIZER = "nfkd-lower-stripmark-cjk-bigram-v4"  # places_pages.rs:67
ADDRESS_NORMALIZATION = "address-transform-v1"  # address_construction_v1.rs:50
MAX_ROUTING_BYTES = 8 * 1024 * 1024  # MAX_PLACES/ADDRESS_ROUTING_BYTES

PLACES_ROUTING = b'{"stub":"places-routing"}\n'
ADDRESSES_ROUTING = b'{"stub":"addresses-routing"}\n'


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def construction_family_manifest(family: str, routing_payload: bytes) -> dict:
    if family == "places":
        versions = {
            "format": PLACES_FORMAT,
            "tokenizer": PLACES_TOKENIZER,
            "normalization": None,
        }
        extra = [
            {
                "object_key": f"families/places/objects/{'a' * 64}.plrv",
                "bytes": 10,
                "sha256": "a" * 64,
            }
        ]
    else:
        versions = {
            "format": ADDRESS_FORMAT,
            "tokenizer": None,
            "normalization": ADDRESS_NORMALIZATION,
        }
        extra = [
            {
                "object_key": f"families/addresses/objects/{'b' * 64}.oav1",
                "bytes": 11,
                "sha256": "b" * 64,
            }
        ]
    return gbm.build_family_manifest(
        family,
        lineage={
            "overture_release": RELEASE,
            "build_id": "c" * 64,
            "producer_commit": "deadbeef",
            "producer_script": "scripts/construction_v1_hosted.py",
            "producer_version": "construction-v1",
        },
        versions=versions,
        region={
            "name": "planet",
            "bbox": [-180.0, -90.0, 180.0, 90.0],
            "bbox_scope": "row_group_approximate",
        },
        artifacts=[
            {
                "object_key": f"families/{family}/routing.json",
                "bytes": len(routing_payload),
                "sha256": _sha(routing_payload),
            },
            *extra,
        ],
        generated_at=None,
    )


def slice_source_manifest(manifests: dict[str, dict]) -> dict:
    summaries = {}
    verified = []
    for family, manifest in sorted(manifests.items()):
        artifacts = manifest["artifacts"]
        href = f"./families/{family}/family-manifest.json"
        summaries[family] = {
            "manifest": href,
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
        verified.append({"href": href})
        verified.extend(summaries[family]["objects"])
    return {
        "schema_version": 1,
        "slice_version": SLICE,
        "overture_release": RELEASE,
        "generated_at": "2026-07-28T00:00:00+00:00",
        "is_slice": True,
        "promotion_eligible": False,
        "families": summaries,
        "verified_version_objects": verified,
    }


class StubClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class StubR2Client:
    """In-memory GetObject/PutObject with conditional headers.

    ETags are single-part content MD5s, exactly what the CAS layer's If-Match
    condition is value-addressed on.
    """

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.log: list[tuple] = []

    @staticmethod
    def _etag(payload: bytes) -> str:
        return '"%s"' % hashlib.md5(payload).hexdigest()

    def seed(self, key: str, payload: bytes) -> None:
        self.objects[key] = payload

    def get_object(self, Bucket, Key):
        self.log.append(("get", Key))
        if Key not in self.objects:
            raise StubClientError("404")
        payload = self.objects[Key]
        return {
            "ContentLength": len(payload),
            "Body": io.BytesIO(payload),
            "ETag": self._etag(payload),
            "Metadata": {},
        }

    def put_object(
        self, Bucket, Key, Body, ContentLength, Metadata, IfNoneMatch=None, IfMatch=None
    ):
        self.log.append(("put", Key, IfNoneMatch, IfMatch))
        assert (IfNoneMatch is None) != (IfMatch is None)
        if IfNoneMatch is not None:
            assert IfNoneMatch == "*"
            if Key in self.objects:
                raise StubClientError("412")
        elif Key not in self.objects or self._etag(self.objects[Key]) != IfMatch:
            raise StubClientError("412")
        payload = Body.read()
        assert len(payload) == ContentLength
        assert Metadata == {"sha256": _sha(payload)}
        self.objects[Key] = payload

def stub_control_store(client: StubR2Client):
    import r2_verified_store as rvs

    store = rvs.Boto3Store.__new__(rvs.Boto3Store)
    store.client = client
    store.bucket = "test-bucket"
    store._client_error = StubClientError
    store._stream_retry_error = type("NeverRetry", (Exception,), {})
    return v2.R2ControlStore(store)


@pytest.fixture()
def world(monkeypatch):
    client = StubR2Client()
    legacy = fixtures.legacy_release(LEGACY, release=RELEASE)
    places = construction_family_manifest("places", PLACES_ROUTING)
    addresses = construction_family_manifest("addresses", ADDRESSES_ROUTING)
    source = slice_source_manifest({"places": places, "addresses": addresses})
    client.seed(f"{LEGACY}/release-manifest.json", gbm.canonical_json(legacy))
    client.seed(f"{SLICE}/slice-manifest.json", gbm.canonical_json(source))
    client.seed(
        f"{SLICE}/families/places/family-manifest.json", gbm.canonical_json(places)
    )
    client.seed(
        f"{SLICE}/families/addresses/family-manifest.json",
        gbm.canonical_json(addresses),
    )
    client.seed(f"{SLICE}/families/places/routing.json", PLACES_ROUTING)
    client.seed(f"{SLICE}/families/addresses/routing.json", ADDRESSES_ROUTING)
    control = stub_control_store(client)
    original = v2.open_control_store
    monkeypatch.setattr(
        v2,
        "open_control_store",
        lambda spec, what: control if spec == "r2:test-bucket" else original(spec, what),
    )
    return client


def assemble(tmp_path, build: str = BUILD, name: str = "release.json") -> Path:
    output = tmp_path / name
    v2.main(
        [
            "assemble",
            "--store",
            "r2:test-bucket",
            "--geocoder-build",
            build,
            "--overture-release",
            RELEASE,
            "--slice-version",
            SLICE,
            "--legacy-core",
            LEGACY,
            "--output",
            str(output),
        ]
    )
    return output


def publish(path: Path, execute: bool = True) -> None:
    args = ["publish-release", "--store", "r2:test-bucket", "--release", str(path)]
    if execute:
        args.append("--execute")
    v2.main(args)


def promote(build: str, *expectation: str, execute: bool = True) -> None:
    args = [
        "promote",
        "--store",
        "r2:test-bucket",
        "--build",
        build,
        "--generated-at",
        "fixed",
        *expectation,
    ]
    if execute:
        args.append("--execute")
    v2.main(args)


# ---------------------------------------------------------------------------
# assemble


def test_assemble_is_deterministic(world, tmp_path):
    first = assemble(tmp_path, name="one.json").read_bytes()
    second = assemble(tmp_path, name="two.json").read_bytes()
    assert first == second
    assert v2.validate_release_manifest(json.loads(first))


def test_assemble_fails_closed_on_entrypoint_byte_drift(world, tmp_path):
    key = f"{SLICE}/families/places/routing.json"
    payload = world.objects[key]
    world.objects[key] = payload[:-2] + b"~" + payload[-1:]
    with pytest.raises(SystemExit, match="do not match the identity"):
        assemble(tmp_path)


def test_assemble_rejects_non_construction_formats(world, tmp_path):
    manifest = fixtures.family_manifest("places", RELEASE)  # legacy PCSH0001
    world.seed(
        f"{SLICE}/families/places/family-manifest.json", gbm.canonical_json(manifest)
    )
    with pytest.raises(SystemExit, match="not a promoted construction format"):
        assemble(tmp_path)


# ---------------------------------------------------------------------------
# publish-release


def test_publish_release_is_create_only_and_verifies_after_write(world, tmp_path):
    path = assemble(tmp_path)
    start = len(world.log)
    publish(path)
    key = f"v2/releases/{BUILD}/release.json"
    assert world.objects[key] == path.read_bytes()
    puts = [entry for entry in world.log[start:] if entry[0] == "put"]
    assert puts == [("put", key, "*", None)]
    # Verify-after-write re-downloaded the written object.
    assert ("get", key) in world.log[start:]


def test_publish_release_dry_run_writes_nothing(world, tmp_path):
    path = assemble(tmp_path)
    publish(path, execute=False)
    assert f"v2/releases/{BUILD}/release.json" not in world.objects


def test_publish_release_identical_rerun_is_idempotent(world, tmp_path):
    path = assemble(tmp_path)
    publish(path)
    start = len(world.log)
    publish(path)
    assert not [entry for entry in world.log[start:] if entry[0] == "put"]


def test_publish_release_conflicts_on_different_bytes(world, tmp_path):
    path = assemble(tmp_path)
    world.seed(f"v2/releases/{BUILD}/release.json", b'{"squatter":1}')
    with pytest.raises(SystemExit, match="exists with different bytes"):
        publish(path)
    assert world.objects[f"v2/releases/{BUILD}/release.json"] == b'{"squatter":1}'


# ---------------------------------------------------------------------------
# promote


def test_promote_expect_absent_then_expect_sha256(world, tmp_path):
    publish(assemble(tmp_path))
    promote(BUILD, "--expect-absent")
    first = world.objects[CATALOG_KEY]
    catalog = v2.validate_catalog(json.loads(first))
    assert catalog["latest"] == BUILD

    publish(assemble(tmp_path, build=NEWER_BUILD, name="newer.json"))
    promote(NEWER_BUILD, "--expect-sha256", _sha(first))
    catalog = v2.validate_catalog(json.loads(world.objects[CATALOG_KEY]))
    assert catalog["latest"] == NEWER_BUILD
    assert [entry["geocoder_build"] for entry in catalog["releases"]] == [
        NEWER_BUILD,
        BUILD,
    ]
    # The swap was conditional on the previous catalog's ETag.
    cas_puts = [
        entry
        for entry in world.log
        if entry[0] == "put" and entry[1] == CATALOG_KEY and entry[3] is not None
    ]
    assert cas_puts and cas_puts[-1][3] == StubR2Client._etag(first)


def test_promote_can_replace_signed_unavailable_state(world, tmp_path):
    publish(assemble(tmp_path))
    promote(BUILD, "--expect-absent")
    current = world.objects[CATALOG_KEY]
    _recover("--unavailable", "--expect-sha256", _sha(current))
    unavailable = world.objects[CATALOG_KEY]

    publish(assemble(tmp_path, build=NEWER_BUILD, name="newer.json"))
    promote(NEWER_BUILD, "--expect-sha256", _sha(unavailable))
    catalog = v2.validate_catalog(json.loads(world.objects[CATALOG_KEY]))
    assert catalog["latest"] == NEWER_BUILD
    assert [entry["geocoder_build"] for entry in catalog["releases"]] == [
        NEWER_BUILD
    ]
    assert world.log[-2][0] == "put"
    assert world.log[-2][3] == StubR2Client._etag(unavailable)


def test_promote_dry_run_writes_nothing(world, tmp_path):
    publish(assemble(tmp_path))
    promote(BUILD, "--expect-absent", execute=False)
    assert CATALOG_KEY not in world.objects


def test_promote_fails_closed_on_expectation_mismatch(world, tmp_path):
    publish(assemble(tmp_path))
    promote(BUILD, "--expect-absent")
    before = world.objects[CATALOG_KEY]
    publish(assemble(tmp_path, build=NEWER_BUILD, name="newer.json"))
    with pytest.raises(SystemExit, match="not the stated expectation"):
        promote(NEWER_BUILD, "--expect-sha256", "0" * 64)
    assert world.objects[CATALOG_KEY] == before


def test_promote_fails_when_absence_expectation_is_wrong(world, tmp_path):
    publish(assemble(tmp_path))
    with pytest.raises(SystemExit, match="rerun with --expect-absent"):
        promote(BUILD, "--expect-sha256", "0" * 64)
    promote(BUILD, "--expect-absent")
    with pytest.raises(SystemExit, match="rerun with --expect-sha256"):
        promote(BUILD, "--expect-absent")


def test_promote_requires_exactly_one_expectation(world, tmp_path):
    with pytest.raises(SystemExit, match="exactly one of"):
        promote(BUILD)
    with pytest.raises(SystemExit, match="exactly one of"):
        promote(BUILD, "--expect-absent", "--expect-sha256", "0" * 64)


def test_cas_put_rejects_a_stale_token(world):
    control = stub_control_store(world)
    world.seed(CATALOG_KEY, b"current")
    with pytest.raises(v2.StateConflict):
        control.put(CATALOG_KEY, b"next", expect=StubR2Client._etag(b"stale"))
    assert world.objects[CATALOG_KEY] == b"current"


# ---------------------------------------------------------------------------
# recover


def _recover(*extra: str, execute: bool = True) -> None:
    args = ["recover", "--store", "r2:test-bucket", "--generated-at", "fixed", *extra]
    if execute:
        args.append("--execute")
    v2.main(args)


def test_recover_repoints_to_a_prior_release(world, tmp_path):
    publish(assemble(tmp_path))
    promote(BUILD, "--expect-absent")
    good = world.objects[CATALOG_KEY]
    publish(assemble(tmp_path, build=NEWER_BUILD, name="newer.json"))
    promote(NEWER_BUILD, "--expect-sha256", _sha(good))

    current = world.objects[CATALOG_KEY]
    _recover("--build", BUILD, "--expect-sha256", _sha(current))
    catalog = v2.validate_catalog(json.loads(world.objects[CATALOG_KEY]))
    assert catalog["latest"] == BUILD
    assert [entry["geocoder_build"] for entry in catalog["releases"]] == [BUILD]
    # The bad release DOCUMENT is never deleted, only un-pointed.
    assert f"v2/releases/{NEWER_BUILD}/release.json" in world.objects


def test_recover_compare_and_swaps_to_signed_unavailable_state(world, tmp_path):
    publish(assemble(tmp_path))
    promote(BUILD, "--expect-absent")
    current = world.objects[CATALOG_KEY]
    _recover("--unavailable", "--expect-sha256", _sha(current))
    unavailable = v2.validate_unavailable_catalog(
        json.loads(world.objects[CATALOG_KEY])
    )
    assert unavailable["previous_catalog_sha256"] == _sha(current)
    assert unavailable["reason"] == v2.UNAVAILABLE_REASON
    cas_puts = [
        entry
        for entry in world.log
        if entry[0] == "put" and entry[1] == CATALOG_KEY and entry[3] is not None
    ]
    assert cas_puts[-1][3] == StubR2Client._etag(current)
    # The release document survives for a later re-promotion.
    assert f"v2/releases/{BUILD}/release.json" in world.objects


def test_recover_can_repoint_from_unavailable_to_named_release(world, tmp_path):
    publish(assemble(tmp_path))
    promote(BUILD, "--expect-absent")
    current = world.objects[CATALOG_KEY]
    _recover("--unavailable", "--expect-sha256", _sha(current))
    unavailable = world.objects[CATALOG_KEY]

    _recover("--build", BUILD, "--expect-sha256", _sha(unavailable))
    catalog = v2.validate_catalog(json.loads(world.objects[CATALOG_KEY]))
    assert catalog["latest"] == BUILD
    assert [entry["geocoder_build"] for entry in catalog["releases"]] == [BUILD]


def test_unavailable_catalog_contract_is_exact_and_production_only():
    unavailable = v2.build_unavailable_catalog(
        previous_catalog_sha256="a" * 64,
        generated_at="fixed",
    )
    assert v2.validate_unavailable_catalog(unavailable) == unavailable
    with pytest.raises(ValueError, match="only in production"):
        v2.validate_unavailable_catalog(
            unavailable, catalog_key="smoketest-v2/run-1/catalog.json"
        )

    extra = {**unavailable, "extra": True}
    with pytest.raises(ValueError, match="fields"):
        v2.validate_unavailable_catalog(extra)
    tampered = {**unavailable, "previous_catalog_sha256": "b" * 64}
    with pytest.raises(ValueError, match="catalog_digest"):
        v2.validate_unavailable_catalog(tampered)


def test_recover_fails_closed_on_expectation_mismatch(world, tmp_path):
    publish(assemble(tmp_path))
    promote(BUILD, "--expect-absent")
    before = world.objects[CATALOG_KEY]
    with pytest.raises(SystemExit, match="not the stated expectation"):
        _recover("--unavailable", "--expect-sha256", "0" * 64)
    assert world.objects[CATALOG_KEY] == before


def test_recover_requires_exactly_one_action(world, tmp_path):
    publish(assemble(tmp_path))
    promote(BUILD, "--expect-absent")
    sha = _sha(world.objects[CATALOG_KEY])
    with pytest.raises(SystemExit, match="exactly one of"):
        _recover("--expect-sha256", sha)
    with pytest.raises(SystemExit, match="exactly one of"):
        _recover("--build", BUILD, "--unavailable", "--expect-sha256", sha)


# ---------------------------------------------------------------------------
# Worker-contract parity for the assembled document (the exact field
# expectations crates/geocoder-worker/src/v2.rs enforces at admission).


def _ascii_printable(value) -> bool:
    if isinstance(value, str):
        return all(character.isascii() and character.isprintable() for character in value)
    if isinstance(value, list):
        return all(_ascii_printable(item) for item in value)
    if isinstance(value, dict):
        return all(
            _ascii_printable(key) and _ascii_printable(item)
            for key, item in value.items()
        )
    return True


def test_assembled_release_matches_worker_admission_contract(world, tmp_path):
    payload = assemble(tmp_path).read_bytes()
    release = json.loads(payload)

    # parse_verified_control_document: sorted compact JSON + newline, SHA-256.
    unsigned = {key: value for key, value in release.items() if key != "release_digest"}
    assert (
        hashlib.sha256(gbm.canonical_json(unsigned)).hexdigest()
        == release["release_digest"]
    )
    assert _ascii_printable(release)  # printable_ascii_document precondition

    assert release["schema"] == "overture-geocoder-v2-release-v1"  # v2.rs:33
    assert release["geocoder_build"] == BUILD
    assert release["overture_release"] == RELEASE
    assert release["data_version"] == {
        "overture_release": RELEASE,
        "geocoder_build": BUILD,
    }
    # validate_release's exact legacy core layout (v2.rs:475-496).
    assert release["legacy_core"]["version"] == LEGACY
    assert release["legacy_core"]["manifest_key"] == f"{LEGACY}/release-manifest.json"
    assert release["legacy_core"]["manifest_sha256"] == _sha(
        world.objects[f"{LEGACY}/release-manifest.json"]
    )
    assert release["legacy_core"]["entrypoints"] == {
        "feature_lookup": f"{LEGACY}/id-collection.json",
        "forward": f"{LEGACY}/collection.json",
        "reverse": f"{LEGACY}/reverse-collection.json",
    }
    # validate_release's derived operations map (v2.rs:511-529).
    assert release["operations"] == {
        "feature_lookup": ["id"],
        "forward": ["divisions", "places"],
        "reverse": ["divisions"],
        "structured_forward": ["addresses"],
    }

    places = release["families"]["places"]
    assert places["source"] == {
        "kind": "family_slice",
        "version": SLICE,
        "manifest_key": f"{SLICE}/slice-manifest.json",  # v2.rs:353-361
        "manifest_sha256": _sha(world.objects[f"{SLICE}/slice-manifest.json"]),
    }
    assert places["manifest_key"] == f"{SLICE}/families/places/family-manifest.json"
    assert places["manifest_sha256"] == _sha(
        world.objects[f"{SLICE}/families/places/family-manifest.json"]
    )
    assert places["versions"] == {
        "format": PLACES_FORMAT,  # v2.rs:397-399 construction lane
        "tokenizer": PLACES_TOKENIZER,  # v2.rs:411
        "normalization": None,  # v2.rs:412
    }
    assert places["operations"] == ["forward"]  # v2.rs:410
    assert set(places["coverage"]) == {"name", "bbox", "bbox_scope"}  # v2.rs:262-292
    entry = places["entrypoints"]["forward"]
    assert entry == {
        "object_key": f"{SLICE}/families/places/routing.json",  # v2.rs:422-424
        "bytes": len(PLACES_ROUTING),
        "sha256": _sha(PLACES_ROUTING),
    }
    assert 0 < entry["bytes"] <= MAX_ROUTING_BYTES  # v2.rs:427-431,449-452

    addresses = release["families"]["addresses"]
    assert addresses["versions"] == {
        "format": ADDRESS_FORMAT,  # v2.rs:400-402
        "tokenizer": None,  # v2.rs:418
        "normalization": ADDRESS_NORMALIZATION,  # v2.rs:403-407,416
    }
    assert addresses["operations"] == ["structured_forward"]  # v2.rs:415
    assert addresses["entrypoints"]["structured_forward"] == {
        "object_key": f"{SLICE}/families/addresses/routing.json",  # v2.rs:432-439
        "bytes": len(ADDRESSES_ROUTING),
        "sha256": _sha(ADDRESSES_ROUTING),
    }
    # Both families agree about the one source version (v2.rs:530-543).
    assert addresses["source"] == places["source"]


def test_promoted_catalog_matches_worker_admission_contract(world, tmp_path):
    payload = assemble(tmp_path).read_bytes()
    release = json.loads(payload)
    publish(assemble(tmp_path))
    promote(BUILD, "--expect-absent")
    catalog = json.loads(world.objects[CATALOG_KEY])

    unsigned = {key: value for key, value in catalog.items() if key != "catalog_digest"}
    assert (
        hashlib.sha256(gbm.canonical_json(unsigned)).hexdigest()
        == catalog["catalog_digest"]
    )
    assert _ascii_printable(catalog)
    assert catalog["schema"] == "overture-geocoder-v2-catalog-v1"  # v2.rs:32
    assert catalog["latest"] == BUILD  # v2.rs:321
    entry = catalog["releases"][0]
    # release_manifest_key_for_catalog pins the production key (v2.rs:294-296).
    assert entry["manifest_key"] == f"v2/releases/{BUILD}/release.json"
    assert entry["manifest_sha256"] == _sha(world.objects[entry["manifest_key"]])
    assert entry["release_digest"] == release["release_digest"]
    assert entry["overture_release"] == RELEASE


def test_promote_fails_closed_on_pinned_source_drift(world, tmp_path):
    publish(assemble(tmp_path))
    # A family manifest rewritten after publish must block promotion: the
    # worker would refuse the same bytes at admission.
    key = f"{SLICE}/families/places/family-manifest.json"
    tampered = json.loads(world.objects[key])
    tampered["generated_at"] = "later"
    world.seed(key, gbm.canonical_json(tampered))
    with pytest.raises(SystemExit, match="do not hash to the release-pinned sha256"):
        promote(BUILD, "--expect-absent")
    assert CATALOG_KEY not in world.objects


# ---------------------------------------------------------------------------
# The local: backend end-to-end (real open_control_store path, no stubs), and
# the documented check-then-act semantics of its CAS emulation.


def seed_local(root: Path) -> None:
    legacy = fixtures.legacy_release(LEGACY, release=RELEASE)
    places = construction_family_manifest("places", PLACES_ROUTING)
    addresses = construction_family_manifest("addresses", ADDRESSES_ROUTING)
    source = slice_source_manifest({"places": places, "addresses": addresses})
    for key, payload in {
        f"{LEGACY}/release-manifest.json": gbm.canonical_json(legacy),
        f"{SLICE}/slice-manifest.json": gbm.canonical_json(source),
        f"{SLICE}/families/places/family-manifest.json": gbm.canonical_json(places),
        f"{SLICE}/families/addresses/family-manifest.json": gbm.canonical_json(
            addresses
        ),
        f"{SLICE}/families/places/routing.json": PLACES_ROUTING,
        f"{SLICE}/families/addresses/routing.json": ADDRESSES_ROUTING,
    }.items():
        path = root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)


def test_local_backend_full_lifecycle(tmp_path):
    root = tmp_path / "store"
    seed_local(root)
    store_spec = f"local:{root}"
    output = tmp_path / "release.json"
    v2.main(
        [
            "assemble",
            "--store",
            store_spec,
            "--geocoder-build",
            BUILD,
            "--overture-release",
            RELEASE,
            "--slice-version",
            SLICE,
            "--legacy-core",
            LEGACY,
            "--output",
            str(output),
        ]
    )
    v2.main(
        [
            "publish-release",
            "--store",
            store_spec,
            "--release",
            str(output),
            "--execute",
        ]
    )
    assert (root / f"v2/releases/{BUILD}/release.json").read_bytes() == (
        output.read_bytes()
    )
    v2.main(
        [
            "promote",
            "--store",
            store_spec,
            "--build",
            BUILD,
            "--expect-absent",
            "--generated-at",
            "fixed",
            "--execute",
        ]
    )
    catalog_path = root / CATALOG_KEY
    catalog = v2.validate_catalog(json.loads(catalog_path.read_bytes()))
    assert catalog["latest"] == BUILD
    v2.main(
        [
            "recover",
            "--store",
            store_spec,
            "--unavailable",
            "--expect-sha256",
            _sha(catalog_path.read_bytes()),
            "--generated-at",
            "fixed",
            "--execute",
        ]
    )
    unavailable = v2.validate_unavailable_catalog(
        json.loads(catalog_path.read_bytes())
    )
    assert unavailable["reason"] == v2.UNAVAILABLE_REASON


def test_local_store_cas_rejects_stale_expectations(tmp_path):
    store = v2.LocalControlStore(tmp_path)
    store.put("v2/catalog.json", b"one")
    with pytest.raises(v2.StateConflict):
        store.put("v2/catalog.json", b"two")  # create-only against existing
    with pytest.raises(v2.StateConflict):
        store.put("v2/catalog.json", b"two", expect=_sha(b"stale"))
    store.put("v2/catalog.json", b"two", expect=_sha(b"one"))
    assert store.get("v2/catalog.json") == (b"two", _sha(b"two"))


# The head.phrp dependency stays pinned to the legacy PCSH0001 format; the
# construction formats require routing.json instead (build_release_manifest's
# format-keyed FAMILY_OPERATION_DEPENDENCIES).
def test_construction_release_requires_routing_dependency():
    places = construction_family_manifest("places", PLACES_ROUTING)
    broken = copy.deepcopy(places)
    broken["artifacts"] = [
        artifact
        for artifact in broken["artifacts"]
        if artifact["object_key"] != "families/places/routing.json"
    ]
    broken["totals"] = {
        "artifacts": len(broken["artifacts"]),
        "bytes": sum(artifact["bytes"] for artifact in broken["artifacts"]),
    }
    del broken["manifest_digest"]
    broken["manifest_digest"] = gbm.digest(broken)
    source = slice_source_manifest({"places": broken})
    legacy = fixtures.legacy_release(LEGACY, release=RELEASE)
    with pytest.raises(ValueError, match="requires manifest artifact"):
        v2.build_release_manifest(
            geocoder_build=BUILD,
            overture_release=RELEASE,
            legacy_release=legacy,
            legacy_manifest_sha256=fixtures.payload_sha(legacy),
            family_manifests={"places": (broken, fixtures.payload_sha(broken))},
            family_source_manifests={
                "places": (source, fixtures.payload_sha(source))
            },
            family_operations={"places": ["forward"]},
            family_entrypoints={
                "places": {"forward": "families/places/routing.json"}
            },
            generated_at="fixed",
        )
