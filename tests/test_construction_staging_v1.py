"""Fail-closed proof for the construction-v1 R2 staging transport.

The map phase's intermediate store no longer travels between phases as a GitHub
artifact; it lives in a run-scoped R2 staging prefix and each phase fetches by
key. That moves a whole class of failure from "the artifact was there or it
wasn't" to "the object was there, complete, and the right bytes -- or it wasn't",
so every one of those cases has a FAILING test here rather than a comment.

Every test uses the filesystem backend, so this whole surface is covered with no
credentials.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


STAGING = _load("staging_test_module", "scripts/construction_staging_v1.py")
ADDRESS = _load("staging_test_address", "scripts/address_construction_v1.py")

DIGEST = "a" * 64


def _store(tmp_path: Path, *, family: str = "places"):
    local = ADDRESS.LocalObjectStore(tmp_path / "local")
    backend = STAGING.staging_backend(store_root=tmp_path / "staging")
    prefix = STAGING.staging_prefix(DIGEST, family)
    return STAGING.StagedObjectStore(local, backend, prefix), tmp_path / "staging"


def _fresh_cache(staged, tmp_path: Path, name: str):
    """A second store over the SAME staging tree with an EMPTY local cache.

    This is the hosted shape: a later phase runs on a fresh runner and must fetch
    what it needs by key, so anything it can only find on local disk is a test
    that proves nothing.
    """
    local = ADDRESS.LocalObjectStore(tmp_path / name)
    return STAGING.StagedObjectStore(local, staged.store, staged.prefix)


# --------------------------------------------------------------------------- #
# key layout
# --------------------------------------------------------------------------- #
def test_the_staging_prefix_matches_the_convention_r2_cleanup_guards():
    # r2-cleanup.yml phase 2 only accepts ^staging/global-v2/[0-9a-f]{64}/$, so a
    # prefix outside that shape becomes debris its guards cannot expire.
    assert (
        STAGING.staging_prefix(DIGEST, "places")
        == f"staging/global-v2/{DIGEST}/construction-v1/places"
    )
    assert STAGING.staging_prefix(DIGEST, "addresses").endswith("/addresses")


@pytest.mark.parametrize(
    "request_sha256",
    ["", "abc", "A" * 64, "g" * 64, DIGEST + "0"],
)
def test_a_non_canonical_request_digest_is_refused(request_sha256):
    with pytest.raises(ValueError):
        STAGING.staging_prefix(request_sha256, "places")


def test_an_unknown_family_is_refused():
    with pytest.raises(ValueError):
        STAGING.staging_prefix(DIGEST, "divisions")


def test_only_content_addressed_keys_carry_a_verifiable_digest():
    packs = f"map/places-v1/packs/sha256/{DIGEST}.parquet"
    assert STAGING.content_addressed_digest(packs) == DIGEST
    # Markers are NOT content-addressed, and nothing may pretend otherwise.
    assert STAGING.content_addressed_digest("map/places-v1/tasks/t/complete.json") is None
    assert STAGING.content_addressed_digest(f"packs/sha256/{'z' * 64}.parquet") is None


def test_a_key_escaping_the_prefix_is_refused(tmp_path):
    staged, _ = _store(tmp_path)
    for key in ("/absolute", "../escape", "a/../../b", ""):
        with pytest.raises(ValueError):
            staged.staging_key(key)


def test_the_backend_selection_is_unambiguous(tmp_path):
    with pytest.raises(ValueError):
        STAGING.staging_backend()
    with pytest.raises(ValueError):
        STAGING.staging_backend(bucket="b")
    with pytest.raises(ValueError):
        STAGING.staging_backend(store_root=tmp_path, bucket="b", endpoint_url="u")


# --------------------------------------------------------------------------- #
# publication and hydration
# --------------------------------------------------------------------------- #
def test_put_content_publishes_create_only_and_a_fresh_phase_hydrates_it(tmp_path):
    staged, staging_root = _store(tmp_path)
    source = tmp_path / "pack.parquet"
    source.write_bytes(b"term rows" * 1000)

    identity = staged.put_content(source, "map/places-v1/packs", ".parquet")
    # The construction key shape is unchanged -- markers already record these.
    assert identity["key"].startswith("map/places-v1/packs/sha256/")
    assert STAGING.content_addressed_digest(identity["key"]) == identity["sha256"]
    assert staged.evidence()["staged_objects_published"] == 1

    consumer = _fresh_cache(staged, tmp_path, "consumer")
    hydrated = consumer.path(identity["key"])
    assert hydrated.read_bytes() == source.read_bytes()
    assert consumer.evidence()["staged_objects_hydrated"] == 1
    # A second read is served from the local cache, not re-fetched.
    consumer.path(identity["key"])
    assert consumer.evidence()["staged_objects_hydrated"] == 1
    assert (staging_root / staged.staging_key(identity["key"])).is_file()


def test_republishing_byte_identical_content_is_a_verified_no_op(tmp_path):
    staged, _ = _store(tmp_path)
    source = tmp_path / "pack.parquet"
    source.write_bytes(b"identical")
    first = staged.put_content(source, "map/places-v1/packs", ".parquet")

    second = _fresh_cache(staged, tmp_path, "rerun")
    again = second.put_content(source, "map/places-v1/packs", ".parquet")
    assert again == first


def test_a_missing_staged_object_aborts_instead_of_falling_back(tmp_path):
    # There is no artifact path to fall back to any more, and a silent fallback is
    # how a partial store becomes a wrong slice.
    staged, staging_root = _store(tmp_path)
    source = tmp_path / "pack.parquet"
    source.write_bytes(b"payload")
    key = staged.put_content(source, "map/places-v1/packs", ".parquet")["key"]

    (staging_root / staged.staging_key(key)).unlink()
    consumer = _fresh_cache(staged, tmp_path, "consumer")
    with pytest.raises(FileNotFoundError):
        consumer.path(key)


def test_a_short_staged_object_aborts(tmp_path):
    staged, staging_root = _store(tmp_path)
    source = tmp_path / "pack.parquet"
    source.write_bytes(b"payload" * 100)
    key = staged.put_content(source, "map/places-v1/packs", ".parquet")["key"]

    target = staging_root / staged.staging_key(key)
    target.write_bytes(source.read_bytes()[:-10])
    consumer = _fresh_cache(staged, tmp_path, "consumer")
    with pytest.raises(ValueError):
        consumer.path(key)
    # Nothing partial is left behind for the next caller to trust.
    assert not consumer.local.path(key).exists()


def test_staged_bytes_that_do_not_hash_to_their_key_abort(tmp_path):
    staged, staging_root = _store(tmp_path)
    source = tmp_path / "pack.parquet"
    source.write_bytes(b"a" * 64)
    key = staged.put_content(source, "map/places-v1/packs", ".parquet")["key"]

    # Same length, different bytes: only the digest catches this.
    (staging_root / staged.staging_key(key)).write_bytes(b"b" * 64)
    consumer = _fresh_cache(staged, tmp_path, "consumer")
    with pytest.raises(ValueError):
        consumer.path(key)


def test_lying_staged_metadata_aborts_before_any_download(tmp_path):
    staged, staging_root = _store(tmp_path)
    source = tmp_path / "pack.parquet"
    source.write_bytes(b"payload")
    key = staged.put_content(source, "map/places-v1/packs", ".parquet")["key"]

    sidecar = staging_root / f"{staged.staging_key(key)}.metadata.json"
    sidecar.write_text(json.dumps({"sha256": "c" * 64}) + "\n")
    consumer = _fresh_cache(staged, tmp_path, "consumer")
    with pytest.raises(ValueError, match="metadata digest differs"):
        consumer.path(key)


def test_a_key_with_no_digest_to_verify_against_is_never_hydrated(tmp_path):
    staged, _ = _store(tmp_path)
    with pytest.raises(ValueError, match="not content-addressed"):
        staged.path("map/places-v1/tasks/places-map-000/complete.json")


# --------------------------------------------------------------------------- #
# markers
# --------------------------------------------------------------------------- #
def test_a_marker_is_durable_and_a_fresh_phase_reads_it_back_verified(tmp_path):
    staged, _ = _store(tmp_path)
    key = "map/places-v1/tasks/places-map-000/complete.json"
    staged.write_marker_last(key, {"schema": "x", "task_id": "places-map-000"})

    consumer = _fresh_cache(staged, tmp_path, "consumer")
    assert consumer.read_json(key) == {"schema": "x", "task_id": "places-map-000"}
    assert consumer.evidence()["staged_objects_hydrated"] == 1


def test_an_absent_marker_reads_as_not_completed(tmp_path):
    # Definitively absent means "not written"; under create-only writes a re-run is
    # safe. A TRANSPORT error must NOT read as absence -- see the S3Store head
    # contract and construction_v1_hosted._remote_marker_completed.
    staged, _ = _store(tmp_path)
    assert staged.read_json("map/places-v1/tasks/nope/complete.json") is None


def test_rewriting_a_marker_with_different_bytes_aborts(tmp_path):
    staged, _ = _store(tmp_path)
    key = "map/places-v1/tasks/places-map-000/complete.json"
    staged.write_marker_last(key, {"records": 1})

    second = _fresh_cache(staged, tmp_path, "rerun")
    with pytest.raises(ValueError):
        second.write_marker_last(key, {"records": 2})
    # The original is intact: create-only means the first writer wins.
    assert _fresh_cache(staged, tmp_path, "reader").read_json(key) == {"records": 1}


def test_rewriting_a_marker_with_identical_bytes_is_a_no_op(tmp_path):
    staged, _ = _store(tmp_path)
    key = "map/places-v1/tasks/places-map-000/complete.json"
    staged.write_marker_last(key, {"records": 1})
    _fresh_cache(staged, tmp_path, "rerun").write_marker_last(key, {"records": 1})


def test_an_unverifiable_staged_marker_aborts_rather_than_being_trusted(tmp_path):
    staged, staging_root = _store(tmp_path)
    key = "map/places-v1/tasks/places-map-000/complete.json"
    staged.write_marker_last(key, {"records": 1})

    (staging_root / f"{staged.staging_key(key)}.metadata.json").unlink()
    consumer = _fresh_cache(staged, tmp_path, "consumer")
    with pytest.raises(ValueError, match="no sha256 metadata"):
        consumer.read_json(key)


def test_a_tampered_staged_marker_aborts(tmp_path):
    staged, staging_root = _store(tmp_path)
    key = "map/places-v1/tasks/places-map-000/complete.json"
    staged.write_marker_last(key, {"records": 1})

    target = staging_root / staged.staging_key(key)
    target.write_bytes(json.dumps({"records": 999}).encode() + b"\n")
    consumer = _fresh_cache(staged, tmp_path, "consumer")
    with pytest.raises(ValueError):
        consumer.read_json(key)


# --------------------------------------------------------------------------- #
# isolation
# --------------------------------------------------------------------------- #
def test_the_two_families_and_two_runs_never_share_a_key_space(tmp_path):
    places, staging_root = _store(tmp_path, family="places")
    addresses = STAGING.StagedObjectStore(
        ADDRESS.LocalObjectStore(tmp_path / "local-addr"),
        places.store,
        STAGING.staging_prefix(DIGEST, "addresses"),
    )
    other_run = STAGING.StagedObjectStore(
        ADDRESS.LocalObjectStore(tmp_path / "local-other"),
        places.store,
        STAGING.staging_prefix("b" * 64, "places"),
    )
    key = "map/x/sha256/" + DIGEST + ".parquet"
    assert len({s.staging_key(key) for s in (places, addresses, other_run)}) == 3
    assert staging_root  # the three prefixes live in one bucket, disjointly
