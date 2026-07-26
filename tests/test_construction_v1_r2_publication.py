"""The real R2 publication backend for finalize, and what must stay true of it.

The shell mirror this replaces was the top blocker for a planet dispatch: a serial
`find | while read` loop spending at least two aws-cli invocations per published
object at 0.339 s of CPU startup each -- 12.4 h for a 65,751-object address slice
and 8.4 h for 44,305 places objects, against a 360-minute job timeout, before a
byte moved. It also wrote the whole slice (~100-145 GB for addresses) to a local
tree first, against a 25 GB free-disk floor, so the address family could not land
at all.

Everything here runs with NO credentials and touches no bucket. Two seams make
that possible and both are used deliberately:

* `r2_verified_store.FilesystemStore` -- a real `ObjectStore`, so
  `VerifiedStoreRemote` (the class a hosted run uses) is exercised end to end
  including its metadata-based whole-slice verification;
* a local stub S3 client for `Boto3Store`, so the create-only, absence and
  multipart-ETag rules of the persistent-client path are pinned against an S3 API
  surface without a live bucket.

Several tests here exist because a correct-but-serial or correct-but-unguarded
publisher passes every OTHER test in the suite. Each one names the mutation it
catches.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


REMOTE = _load("r2_publication_remote", "scripts/construction_v1_remote.py")
R2 = _load("r2_publication_store", "scripts/r2_verified_store.py")
HOSTED = _load("r2_publication_hosted", "scripts/construction_v1_hosted.py")

PREFIX = "construction-v1/binding/slice/a/"
MARKER = "construction-v1/binding/markers/finalize.json"


def _budget(**overrides):
    caps = {"max_operations": 100_000, "max_write_bytes": 10**9, "max_read_bytes": 10**9}
    caps.update(overrides)
    return REMOTE.Budget(**caps)


def _store_remote(tmp_path, *, budget=None):
    """`VerifiedStoreRemote` over a real filesystem `ObjectStore`."""
    store = R2.FilesystemStore(tmp_path / "bucket")
    return REMOTE.VerifiedStoreRemote(store, budget or _budget()), store


def _objects(tmp_path, count=24, size=4096):
    source = tmp_path / "source"
    source.mkdir(parents=True, exist_ok=True)
    artifacts = []
    for index in range(count):
        path = source / f"{index:04d}"
        path.write_bytes(bytes([index % 251]) * size)
        artifacts.append((f"{PREFIX}{index:04d}", path))
    return artifacts


# --------------------------------------------------------------------------- #
# 1. The real backend publishes the exact set and verifies it without a re-read
# --------------------------------------------------------------------------- #
def test_the_store_backed_remote_publishes_the_exact_set_and_verifies_it(tmp_path):
    artifacts = _objects(tmp_path)
    remote, store = _store_remote(tmp_path)
    marker = REMOTE.publish_exact_set(
        remote, artifacts=artifacts, marker_key=MARKER, request_sha256="a" * 64
    )
    assert len(marker["artifacts"]) == len(artifacts)
    verification = REMOTE.verify_whole_slice_once(
        remote, prefix=PREFIX, expected=marker["artifacts"]
    )
    assert verification["objects"] == len(artifacts)
    # The bytes really are in the store, under the keys the marker names.
    for item in marker["artifacts"]:
        info = store.head(str(item["key"]))
        assert info.bytes == item["bytes"]
        assert info.sha256 == item["sha256"]


def test_whole_slice_verification_does_not_re_download_the_slice(tmp_path):
    """The biggest cost in the phase, and the reason it is a backend decision.

    Streaming every final object is a full re-download of the published slice --
    13-18 GB for planet places, ~100-145 GB for addresses -- on top of the hydration
    and the upload. `VerifiedStoreRemote` verifies from one metadata request instead.
    Assert it by counting GETs: a metadata-verified slice performs NONE, and the
    remote's read-byte budget stays at zero.

    Reverting `read_back_identity` to `_stream_identity` -- the obvious "simplify"
    -- leaves every correctness test in this file passing and puts tens of minutes
    back into the planet phase. This is what notices.
    """
    artifacts = _objects(tmp_path)
    gets: list[str] = []

    class _CountingStore(R2.FilesystemStore):
        def open_stream(self, key):
            gets.append(key)
            return super().open_stream(key)

    budget = _budget()
    remote = REMOTE.VerifiedStoreRemote(_CountingStore(tmp_path / "bucket"), budget)
    marker = REMOTE.publish_exact_set(
        remote, artifacts=artifacts, marker_key=MARKER, request_sha256="b" * 64
    )
    assert gets == []
    before = budget.read_bytes
    verification = REMOTE.verify_whole_slice_once(
        remote, prefix=PREFIX, expected=marker["artifacts"]
    )
    assert verification["objects"] == len(artifacts)
    assert gets == [], f"whole-slice verification downloaded {len(gets)} objects"
    assert budget.read_bytes == before == 0
    # And the operation count is unchanged by the switch: one metadata request per
    # object is what one streaming read used to charge, so #173's projection holds
    # exactly. Measured against the real projection, not against a re-derivation.
    assert budget.operations == HOSTED.finalize_remote_operations(
        len(artifacts), retried=False
    )


def test_metadata_verification_catches_a_substituted_published_object(tmp_path):
    """Not a re-download, but still a real check on the STORED bytes.

    The store computes the content digest itself (the single-part ETag on R2, the
    file's own MD5 here), so replacing a published object behind the store's back
    must still be caught. If this passed, the cheap verification would be no
    verification.
    """
    artifacts = _objects(tmp_path, count=4)
    remote, store = _store_remote(tmp_path)
    marker = REMOTE.publish_exact_set(
        remote, artifacts=artifacts, marker_key=MARKER, request_sha256="c" * 64
    )
    victim = str(marker["artifacts"][2]["key"])
    target = tmp_path / "bucket" / victim
    # Same LENGTH, so nothing that compares sizes would notice, and the sha256
    # metadata sidecar is left in place so the recorded identity still "agrees".
    target.write_bytes(b"Z" * target.stat().st_size)
    with pytest.raises(RuntimeError, match="final slice identity differs"):
        REMOTE.verify_whole_slice_once(
            remote, prefix=PREFIX, expected=marker["artifacts"]
        )


def test_metadata_verification_also_cross_checks_the_recorded_sha256(tmp_path):
    """The second comparison, which the content digest cannot stand in for.

    `read_back_identity` checks two independent things: the store's own content digest
    against the bytes that were sent, and the store's RECORDED sha256 against the
    admitted identity. The first proves the object holds the published bytes; the
    second proves the metadata a later consumer reads describes the same object.
    Nothing else covers the second -- `verify_whole_slice_once` compares the returned
    identity to `expected`, and the returned identity is built FROM `expected`, so a
    consumer that trusts the recorded digest would be trusting an unchecked value.

    Tampered here by rewriting only the sidecar, leaving the bytes alone, so the
    content-digest comparison passes and only this check can fail.
    """
    artifacts = _objects(tmp_path, count=3)
    remote, _ = _store_remote(tmp_path)
    marker = REMOTE.publish_exact_set(
        remote, artifacts=artifacts, marker_key=MARKER, request_sha256="a" * 64
    )
    victim = str(marker["artifacts"][1]["key"])
    sidecar = tmp_path / "bucket" / f"{victim}.metadata.json"
    sidecar.write_text(json.dumps({"sha256": "b" * 64}) + "\n")
    # The bytes are untouched, so the content digest still matches what was sent.
    proof = remote.store.head_proof(victim)
    assert proof["content_md5"] == remote._sent_md5[victim]
    with pytest.raises(RuntimeError, match="recorded sha256 differs"):
        REMOTE.verify_whole_slice_once(
            remote, prefix=PREFIX, expected=marker["artifacts"]
        )


def test_a_resumed_verification_with_no_sent_digest_falls_back_to_a_full_read(tmp_path):
    """No local evidence means do the expensive honest thing, never skip the check.

    On a resumed finalize the objects were published by an earlier process, so there
    is no captured sent-MD5 to compare the store's digest against. That must fall
    back to the full streaming read-back rather than pass.
    """
    artifacts = _objects(tmp_path, count=4)
    remote, _ = _store_remote(tmp_path)
    marker = REMOTE.publish_exact_set(
        remote, artifacts=artifacts, marker_key=MARKER, request_sha256="d" * 64
    )
    # A FRESH remote over the same bucket: same objects, no memory of sending them.
    fresh = REMOTE.VerifiedStoreRemote(R2.FilesystemStore(tmp_path / "bucket"), _budget())
    assert fresh._sent_md5 == {}
    verified = REMOTE.verify_whole_slice_once(
        fresh, prefix=PREFIX, expected=marker["artifacts"]
    )
    assert verified["objects"] == len(artifacts)
    # And the fallback is a real read-back, not a rubber stamp.
    victim = str(marker["artifacts"][1]["key"])
    target = tmp_path / "bucket" / victim
    target.write_bytes(b"Y" * target.stat().st_size)
    another = REMOTE.VerifiedStoreRemote(R2.FilesystemStore(tmp_path / "bucket"), _budget())
    with pytest.raises(RuntimeError, match="final slice identity differs"):
        REMOTE.verify_whole_slice_once(
            another, prefix=PREFIX, expected=marker["artifacts"]
        )


def test_a_resumed_verification_costs_the_operations_the_projection_prices(tmp_path):
    """The fallback must cost ONE operation per object, like the fast path.

    A resumed finalize is priced at 4 operations per object: PUT attempt, the
    conflict's byte-exactness read, the per-upload HEAD, and one verification read.
    The streaming fallback is that fourth one -- so looking up the sent digest AFTER
    the metadata request, and then falling back, charges FIVE. On a planet slice that
    is a 25% overrun on `max_remote_operations`, discovered inside `Budget.charge`
    part-way through the phase, which is exactly the failure #173's plan-time gate
    exists to make impossible.
    """
    artifacts = _objects(tmp_path, count=8)
    first, _ = _store_remote(tmp_path)
    marker = REMOTE.publish_exact_set(
        first, artifacts=artifacts, marker_key=MARKER, request_sha256="7" * 64
    )
    # A resumed process: same bucket, no memory of sending anything, so every put
    # conflicts and every verification takes the streaming fallback.
    budget = _budget()
    resumed = REMOTE.VerifiedStoreRemote(R2.FilesystemStore(tmp_path / "bucket"), budget)
    REMOTE.publish_exact_set(
        resumed, artifacts=artifacts, marker_key=MARKER, request_sha256="7" * 64
    )
    REMOTE.verify_whole_slice_once(
        resumed, prefix=PREFIX, expected=marker["artifacts"]
    )
    assert budget.operations == HOSTED.finalize_remote_operations(len(artifacts))


def test_the_upload_body_can_be_rewound_for_a_retry_and_the_digest_follows(tmp_path):
    """botocore rewinds a request body to retry; the digest must describe what was SENT.

    A body with no `seek` makes a retryable 5xx fatal -- strictly worse than the
    aws-cli path, part-way through publishing tens of thousands of objects. And a
    digest carried across the rewind would describe bytes that never arrived, so the
    whole-slice comparison would fail on a run that in fact published correctly.
    """
    source = _write(tmp_path / "body", b"payload-bytes" * 100)
    reader = REMOTE._MD5Reader(source.open("rb"))
    reader.read(64)
    assert reader.tell() == 64
    # The retry: rewind and send it all.
    assert reader.seek(0) == 0
    assert reader.read() == source.read_bytes()
    assert reader.hexdigest() == hashlib.md5(source.read_bytes()).hexdigest()
    # A PARTIAL rewind cannot be accounted for and must refuse rather than guess.
    reader.seek(0)
    reader.read(32)
    with pytest.raises(RuntimeError, match="partial rewind"):
        reader.seek(16)

    # And it works through a real publication whose store rewinds every body.
    class _RewindingStore(R2.FilesystemStore):
        def upload_fileobj(self, source, key, sha256, *, size):
            source.read(8)
            source.seek(0)
            return super().upload_fileobj(source, key, sha256, size=size)

    remote = REMOTE.VerifiedStoreRemote(_RewindingStore(tmp_path / "bucket"), _budget())
    artifacts = _objects(tmp_path, count=4)
    marker = REMOTE.publish_exact_set(
        remote, artifacts=artifacts, marker_key=MARKER, request_sha256="8" * 64
    )
    assert REMOTE.verify_whole_slice_once(
        remote, prefix=PREFIX, expected=marker["artifacts"]
    )["objects"] == 4


def test_the_pool_never_has_more_than_the_bound_submitted_or_running(tmp_path, monkeypatch):
    """The bound is on the EXECUTOR, and submission is bounded too -- pin both.

    These are different claims and only one of them is load-bearing, which is why
    both need saying. `max_workers` is what bounds concurrent execution, and therefore
    residency, RAM and connections; submitting the whole set ahead of time would still
    be execution-bounded, so a test that only measured residency would not notice it.
    `_run_bounded` is written to do neither, and this asserts it by counting live
    submissions.
    """
    submitted = [0]
    outstanding = [0]
    peak_outstanding = [0]
    lock = threading.Lock()
    real_executor = REMOTE.ThreadPoolExecutor

    class _CountingExecutor(real_executor):
        def submit(self, function, *args, **kwargs):
            with lock:
                submitted[0] += 1
                outstanding[0] += 1
                peak_outstanding[0] = max(peak_outstanding[0], outstanding[0])
            future = super().submit(function, *args, **kwargs)
            future.add_done_callback(lambda _f: _decrement())
            return future

    def _decrement():
        with lock:
            outstanding[0] -= 1

    monkeypatch.setattr(REMOTE, "ThreadPoolExecutor", _CountingExecutor)
    items = list(range(4 * REMOTE.PUBLISH_CONCURRENCY + 7))
    REMOTE._run_bounded(lambda _i, _x: None, items, concurrency=REMOTE.PUBLISH_CONCURRENCY)
    assert submitted[0] == len(items)
    # Not vacuous: the set is several times the bound.
    assert len(items) > 4 * REMOTE.PUBLISH_CONCURRENCY
    assert peak_outstanding[0] <= REMOTE.PUBLISH_CONCURRENCY, (
        f"{peak_outstanding[0]} futures outstanding against a bound of "
        f"{REMOTE.PUBLISH_CONCURRENCY}"
    )


def test_the_executor_is_created_with_the_concurrency_bound(tmp_path, monkeypatch):
    """The bound that actually limits concurrent execution, asserted where it is set.

    `max_workers` is the one that decides residency, RAM and open connections.
    Dropping it (a bare `ThreadPoolExecutor()`) leaves the publisher correct and
    silently raises the peak to `min(32, cpu+4)`, which no output-shaped test can see.
    """
    seen: list[int | None] = []
    real_executor = REMOTE.ThreadPoolExecutor

    class _RecordingExecutor(real_executor):
        def __init__(self, *args, max_workers=None, **kwargs):
            seen.append(max_workers)
            super().__init__(*args, max_workers=max_workers, **kwargs)

    monkeypatch.setattr(REMOTE, "ThreadPoolExecutor", _RecordingExecutor)
    artifacts = _objects(tmp_path, count=4)
    remote, _ = _store_remote(tmp_path)
    marker = REMOTE.publish_exact_set(
        remote, artifacts=artifacts, marker_key=MARKER, request_sha256="9" * 64
    )
    REMOTE.verify_whole_slice_once(remote, prefix=PREFIX, expected=marker["artifacts"])
    assert seen, "the publisher did not build a pool at all"
    assert set(seen) == {REMOTE.PUBLISH_CONCURRENCY}


def test_the_per_upload_head_compares_the_digest_on_a_store_that_records_one(tmp_path):
    """Property 3 is a DIGEST comparison on the real backend, not a length check.

    `FilesystemRemote` can only compare lengths, which is why the payload re-check in
    the upload pass matters so much there. A store that records the sha256 gives a
    stronger per-upload check, and `records_sha256_metadata` is what selects it --
    declared per backend rather than defaulted, so a backend that forgets cannot
    silently get the weaker one.
    """
    assert REMOTE.VerifiedStoreRemote.records_sha256_metadata is True
    assert REMOTE.FilesystemRemote.records_sha256_metadata is False
    item = {"key": "k", "sha256": "f" * 64, "bytes": 7}
    remote, _ = _store_remote(tmp_path)
    assert REMOTE._expected_head(remote, item) == {"bytes": 7, "sha256": "f" * 64}
    filesystem = REMOTE.FilesystemRemote(tmp_path / "tree", _budget())
    assert REMOTE._expected_head(filesystem, item) == {"bytes": 7}

    # An object whose recorded digest is missing FAILS the comparison rather than
    # passing it, which is the direction that matters.
    artifacts = _objects(tmp_path, count=1)
    published = REMOTE.publish_exact_set(
        remote, artifacts=artifacts, marker_key=MARKER, request_sha256="e" * 64
    )
    key = str(published["artifacts"][0]["key"])
    (tmp_path / "bucket" / f"{key}.metadata.json").unlink()
    assert remote.head(key) == {"bytes": published["artifacts"][0]["bytes"]}
    assert remote.head(key) != REMOTE._expected_head(remote, published["artifacts"][0])


# --------------------------------------------------------------------------- #
# 2. The marker is last, and a mid-run failure leaves none
# --------------------------------------------------------------------------- #
def test_the_marker_is_the_last_object_written_even_under_concurrency(tmp_path):
    """Ordering that concurrency could plausibly break, asserted on the backend.

    Every worker in the pool writes objects; the marker is written by the caller
    afterwards. If the marker moved into the loop, or if the pool were still draining
    when it was committed, the marker would stop meaning "the whole admitted set is
    published". Record the order the BACKEND saw and require the marker last.
    """
    artifacts = _objects(tmp_path, count=40)
    order: list[str] = []
    lock = threading.Lock()

    class _RecordingStore(R2.FilesystemStore):
        def upload_fileobj(self, source, key, sha256, *, size):
            result = super().upload_fileobj(source, key, sha256, size=size)
            with lock:
                order.append(key)
            return result

    remote = REMOTE.VerifiedStoreRemote(_RecordingStore(tmp_path / "bucket"), _budget())
    marker = REMOTE.publish_exact_set(
        remote, artifacts=artifacts, marker_key=MARKER, request_sha256="1" * 64
    )
    assert len(order) == len(artifacts) + 1
    assert order[-1] == MARKER
    assert MARKER not in order[:-1]
    assert sorted(order[:-1]) == sorted(str(i["key"]) for i in marker["artifacts"])


def test_a_mid_run_failure_under_concurrency_leaves_no_marker(tmp_path):
    """An incomplete slice must never carry a completion marker.

    The pool makes this less obvious than it was: several uploads are in flight when
    one fails, and the publisher has to drain them and refuse to commit rather than
    reach the marker anyway. 40 members with 16 workers so the failure really does
    happen with others still running.
    """
    artifacts = _objects(tmp_path, count=40)
    remote, store = _store_remote(tmp_path)
    with pytest.raises(RuntimeError, match="injected interruption"):
        REMOTE.publish_exact_set(
            remote, artifacts=artifacts, marker_key=MARKER,
            request_sha256="2" * 64, fail_after_upload=5,
        )
    assert store.head(MARKER) is None
    # Some objects DID land -- otherwise this proves nothing about draining -- and the
    # slice is therefore incomplete, which is exactly why the marker must be absent.
    landed = store.list_prefix(PREFIX)
    assert 0 < len(landed) < len(artifacts)
    # And the resume completes it, byte-safe, marker last.
    resumed = REMOTE.publish_exact_set(
        remote, artifacts=artifacts, marker_key=MARKER, request_sha256="2" * 64
    )
    assert store.head(MARKER) is not None
    assert len(resumed["artifacts"]) == len(artifacts)


def test_a_failing_member_stops_submission_and_still_drains_the_pool(tmp_path):
    """The marker is unreachable while any upload is in flight.

    `_run_bounded` must stop submitting on the first failure AND await what is
    already running before it raises. If it raised while workers were live, the
    caller could commit a marker over a still-moving slice.
    """
    artifacts = _objects(tmp_path, count=40)
    live = [0]
    peak_live_at_raise = [0]
    lock = threading.Lock()

    class _SlowFailingStore(R2.FilesystemStore):
        def upload_fileobj(self, source, key, sha256, *, size):
            with lock:
                live[0] += 1
            try:
                if key.endswith("0007"):
                    raise RuntimeError("upload exploded")
                return super().upload_fileobj(source, key, sha256, size=size)
            finally:
                with lock:
                    live[0] -= 1
                    peak_live_at_raise[0] = max(peak_live_at_raise[0], live[0])

    remote = REMOTE.VerifiedStoreRemote(_SlowFailingStore(tmp_path / "bucket"), _budget())
    with pytest.raises(RuntimeError, match="upload exploded"):
        REMOTE.publish_exact_set(
            remote, artifacts=artifacts, marker_key=MARKER, request_sha256="3" * 64
        )
    # No marker, and not every member was even attempted: submission stopped.
    assert R2.FilesystemStore(tmp_path / "bucket").head(MARKER) is None
    assert len(R2.FilesystemStore(tmp_path / "bucket").list_prefix(PREFIX)) < len(artifacts)
    # By the time the exception surfaced, nothing was running.
    assert live[0] == 0


# --------------------------------------------------------------------------- #
# 3. The conflict path compares BYTES, including for the keys that are not
#    content-addressed -- the shell mirror's tracked defect
# --------------------------------------------------------------------------- #
def test_a_conflicting_non_content_addressed_object_is_refused_on_bytes(tmp_path):
    """The mirror's `|| head-object` fallback accepted existence; this compares bytes.

    For a content-addressed key the name proves the bytes, so accepting a
    pre-existing object was nearly harmless. `family-manifest.json`,
    `slice-manifest.json` and the completion marker are NOT content-addressed: a
    differing pre-existing object was accepted there as a successful publish. That
    was logged as item (5) of the R2-staging follow-ups and is fixed here, so it needs
    a test whose key carries no digest at all.
    """
    manifest = tmp_path / "family-manifest.json"
    manifest.write_bytes(b'{"schema":"construction-v1-family-manifest-v1"}\n')
    key = f"{PREFIX}family-manifest.json"
    remote, store = _store_remote(tmp_path)
    # Someone else's differing manifest is already there, same length.
    squatter = b'{"schema":"construction-v1-family-manifsst-v1"}\n'
    assert len(squatter) == manifest.stat().st_size
    store.upload(_write(tmp_path / "squatter", squatter), key, hashlib.sha256(squatter).hexdigest())
    with pytest.raises(RuntimeError, match="conflicting immutable object"):
        REMOTE.publish_exact_set(
            remote, artifacts=[(key, manifest)], marker_key=MARKER, request_sha256="4" * 64
        )
    # And no marker was committed over the wrong manifest.
    assert store.head(MARKER) is None
    # A byte-IDENTICAL pre-existing object is the resume case and is accepted.
    fresh, fresh_store = _store_remote(tmp_path / "second")
    fresh_store.upload(manifest, key, hashlib.sha256(manifest.read_bytes()).hexdigest())
    assert REMOTE.publish_exact_set(
        fresh, artifacts=[(key, manifest)], marker_key=MARKER, request_sha256="4" * 64
    )["artifacts"][0]["key"] == key


def test_a_conflicting_marker_with_different_bytes_is_refused(tmp_path):
    """The marker is the other non-content-addressed key, and the same rule applies."""
    artifacts = _objects(tmp_path, count=2)
    remote, store = _store_remote(tmp_path)
    store.upload(
        _write(tmp_path / "other-marker", b'{"schema":"someone-elses-marker"}\n'),
        MARKER,
        "0" * 64,
    )
    with pytest.raises(RuntimeError, match="conflicting completion marker"):
        REMOTE.publish_exact_set(
            remote, artifacts=artifacts, marker_key=MARKER, request_sha256="5" * 64
        )


def _write(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


# --------------------------------------------------------------------------- #
# 4. The payload re-check survives the move to streaming uploads
# --------------------------------------------------------------------------- #
def test_the_payload_recheck_still_refuses_a_same_length_swap_on_the_real_backend(tmp_path):
    """#170's invariant, on the backend that actually publishes.

    The upload pass streams rather than reading the payload whole, so the re-check had
    to move from `sha256(read_bytes())` to a digest over the same open handle it then
    uploads from. Its whole reason for existing is unchanged: without it the marker
    records the ADMITTED identity over the UPLOADED bytes.
    """
    source = tmp_path / "object"
    admitted = b"A" * 8192
    source.write_bytes(admitted)
    key = f"{PREFIX}same-length"
    remote, store = _store_remote(tmp_path)
    with pytest.raises(RuntimeError, match="payload changed between admission and upload"):
        REMOTE.publish_exact_set(
            remote, artifacts=[(key, source)], marker_key=MARKER, request_sha256="6" * 64,
            verify=lambda _k, _i: source.write_bytes(b"B" * len(admitted)),
        )
    assert store.head(key) is None
    assert store.head(MARKER) is None


# --------------------------------------------------------------------------- #
# 5. The persistent client, against a local stub S3 surface
# --------------------------------------------------------------------------- #
class _StubS3Error(Exception):
    def __init__(self, code: str, status: int):
        super().__init__(code)
        self.response = {
            "Error": {"Code": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        }


class _StubS3Client:
    """Enough of the S3 API to pin create-only, absence and listing behaviour.

    A local stub rather than a live bucket, deliberately: the rules being pinned are
    the ones that decide whether a planet publication is safe, and they must be
    testable without credentials.
    """

    def __init__(self, *, etag_suffix: str = ""):
        self.objects: dict[str, dict] = {}
        self.etag_suffix = etag_suffix
        self.page_size = 2

    def put_object(self, *, Bucket, Key, Body, ContentLength, Metadata, IfNoneMatch=None):
        if IfNoneMatch == "*" and Key in self.objects:
            raise _StubS3Error("PreconditionFailed", 412)
        payload = Body.read()
        assert len(payload) == ContentLength, "ContentLength must match the body"
        self.objects[Key] = {"body": payload, "metadata": dict(Metadata)}
        return {"ETag": f'"{hashlib.md5(payload).hexdigest()}{self.etag_suffix}"'}

    def head_object(self, *, Bucket, Key):
        if Key not in self.objects:
            raise _StubS3Error("404", 404)
        item = self.objects[Key]
        return {
            "ContentLength": len(item["body"]),
            "Metadata": item["metadata"],
            "ETag": f'"{hashlib.md5(item["body"]).hexdigest()}{self.etag_suffix}"',
        }

    def get_object(self, *, Bucket, Key):
        if Key not in self.objects:
            raise _StubS3Error("NoSuchKey", 404)
        import io

        body = self.objects[Key]["body"]
        return {"ContentLength": len(body), "Body": io.BytesIO(body)}

    def list_objects_v2(self, *, Bucket, Prefix, MaxKeys, ContinuationToken=None):
        keys = sorted(key for key in self.objects if key.startswith(Prefix))
        start = int(ContinuationToken) if ContinuationToken else 0
        page = keys[start : start + self.page_size]
        end = start + len(page)
        truncated = end < len(keys)
        payload = {
            "Contents": [{"Key": key} for key in page],
            "IsTruncated": truncated,
        }
        if truncated:
            payload["NextContinuationToken"] = str(end)
        return payload


def _boto_store(monkeypatch, **stub_kwargs):
    store = R2.Boto3Store.__new__(R2.Boto3Store)
    store.bucket = "bucket"
    store.endpoint_url = "https://example.invalid"
    store._client_error = _StubS3Error
    store.client = _StubS3Client(**stub_kwargs)
    return store


def test_the_persistent_client_is_create_only_and_reports_absence_precisely(tmp_path, monkeypatch):
    store = _boto_store(monkeypatch)
    source = _write(tmp_path / "payload", b"published bytes")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    assert store.head("k") is None
    store.upload(source, "k", digest)
    info = store.head("k")
    assert (info.bytes, info.sha256) == (source.stat().st_size, digest)
    # Second create is a conflict, surfaced as FileExistsError exactly as S3Store does,
    # which is what `VerifiedStoreRemote` turns into ConflictError.
    with pytest.raises(FileExistsError):
        store.upload(source, "k", digest)
    # A non-404 failure must RAISE rather than read as absence -- otherwise a flaky
    # transport silently re-publishes over create-only discipline.
    def _boom(**_kwargs):
        raise _StubS3Error("InternalError", 500)

    store.client.head_object = _boom
    with pytest.raises(RuntimeError, match="head-object failed"):
        store.head("k")


def test_the_persistent_client_paginates_through_the_shared_validator(tmp_path, monkeypatch):
    store = _boto_store(monkeypatch)
    for index in range(7):
        store.upload(_write(tmp_path / f"o{index}", bytes([index]) * 8), f"p/{index}", "0" * 64)
    # Page size 2 over 7 keys: this really exercises continuation.
    assert store.list_prefix("p/") == [f"p/{index}" for index in range(7)]
    # And the fail-closed rules are the SAME function both S3 backends use.
    with pytest.raises(ValueError, match="escaped its requested prefix"):
        R2._validate_list_page({"Contents": [{"Key": "elsewhere"}], "IsTruncated": False},
                               "p/", [], set(), set())
    with pytest.raises(ValueError, match="duplicate key"):
        R2._validate_list_page({"Contents": [{"Key": "p/1"}], "IsTruncated": False},
                               "p/", [], {"p/1"}, set())
    with pytest.raises(ValueError, match="no fresh continuation token"):
        R2._validate_list_page({"Contents": [], "IsTruncated": True}, "p/", [], set(), set())
    with pytest.raises(ValueError, match="invalid contents/truncation"):
        R2._validate_list_page({"Contents": [], "IsTruncated": "no"}, "p/", [], set(), set())


def test_a_multipart_etag_fails_closed_rather_than_verifying_nothing(tmp_path, monkeypatch):
    """A multipart ETag is not the MD5 of the content, so the comparison must abort.

    Publication goes through `put_object`, which is always single-part, so this cannot
    happen today. It is pinned because the failure mode of getting it wrong is a
    verification that silently stops verifying -- the ETag would simply never match,
    or worse, be compared to nothing.
    """
    store = _boto_store(monkeypatch, etag_suffix="-3")
    store.upload(_write(tmp_path / "big", b"x" * 32), "multi", "1" * 64)
    with pytest.raises(RuntimeError, match="MULTIPART ETag"):
        store.head_proof("multi")
    # And the single-part shape is accepted, so the guard is not vacuous.
    single = _boto_store(monkeypatch)
    single.upload(_write(tmp_path / "small", b"x" * 32), "single", "1" * 64)
    proof = single.head_proof("single")
    assert proof["content_md5"] == hashlib.md5(b"x" * 32).hexdigest()
    assert proof["bytes"] == 32


@pytest.mark.parametrize(
    "etag,match",
    [
        (None, "no ETag"),
        ("", "no ETag"),
        ('"abc-2"', "MULTIPART"),
        ('"nothex"', "non-MD5"),
        ('"' + "0" * 31 + '"', "non-MD5"),
    ],
)
def test_the_etag_guard_rejects_everything_that_is_not_a_content_md5(etag, match):
    with pytest.raises(RuntimeError, match=match):
        R2.single_part_etag_md5("k", etag)
    assert R2.single_part_etag_md5("k", '"' + "a" * 32 + '"') == "a" * 32


def test_the_persistent_client_is_the_only_s3_producer_construction_uses(tmp_path):
    """No construction phase may end up on the per-invocation aws-cli path by accident.

    `aws` v2 costs 0.339 s of CPU per invocation and finalize makes two calls per
    published object; the staging store makes one per hydration. Both producers must
    therefore hand back the persistent client.
    """
    source = (ROOT / "scripts/construction_staging_v1.py").read_text()
    assert "R2.s3_object_store(bucket, endpoint_url)" in source
    assert "R2.S3Store(" not in source
    hosted = (ROOT / "scripts/construction_v1_hosted.py").read_text()
    assert "STAGING.R2.s3_object_store(bucket, endpoint_url)" in hosted
    with pytest.raises(ValueError, match="bucket and an endpoint"):
        R2.s3_object_store("", "https://example.invalid")


# --------------------------------------------------------------------------- #
# 6. The finalize CALL SITE, which is where three PRs today shipped holes
# --------------------------------------------------------------------------- #
def test_finalize_refuses_both_publication_targets_and_refuses_neither():
    """Not a permissive default in either direction.

    argparse cannot express "exactly one of one flag and a pair", so the check is in
    `_publication_remote` and has to be a real one: defaulting to the local tree when
    a bucket was asked for would publish a planet slice to a runner's disk and report
    success.
    """
    import argparse

    budget = _budget()

    def _remote(**kwargs):
        namespace = argparse.Namespace(
            remote_root=None, remote_bucket=None, remote_endpoint_url=None
        )
        for key, value in kwargs.items():
            setattr(namespace, key, value)
        return HOSTED._publication_remote(namespace, budget)

    with pytest.raises(SystemExit, match="EITHER --remote-root"):
        _remote(remote_root="tree", remote_bucket="b", remote_endpoint_url="u")
    with pytest.raises(SystemExit, match="needs a publication target"):
        _remote()
    with pytest.raises(SystemExit, match="needs a publication target"):
        _remote(remote_bucket="b")
    with pytest.raises(SystemExit, match="needs a publication target"):
        _remote(remote_endpoint_url="u")
    # Compared by name: `construction_v1_hosted` loads the primitives under its own
    # module name, so the classes are distinct objects for the same source.
    assert type(_remote(remote_root="tree")).__name__ == "FilesystemRemote"


def test_finalize_builds_its_remote_through_the_selector_not_a_hardcoded_backend():
    """The call site, pinned. A helper with good tests behind an unguarded call site
    is the hole that has already shipped three times on this workflow."""
    import inspect

    source = inspect.getsource(HOSTED.cmd_finalize)
    assert "_publication_remote(args, budget)" in source
    # The old form: a local tree, unconditionally, whatever the flags said.
    assert "REMOTE.FilesystemRemote(Path(args.remote_root)" not in source
    # And the workflow really selects the bucket, so the R2 path is the one a planet
    # dispatch takes rather than dead code.
    workflow = (ROOT / ".github/workflows/construction-v1.yml").read_text()
    assert "--remote-bucket" in workflow and "--remote-endpoint-url" in workflow


def test_the_publish_concurrency_bound_is_wired_and_named():
    """A worker pool with an unbounded submission is not a bound.

    `PUBLISH_CONCURRENCY` has to be the DEFAULT the publisher uses, not merely a
    constant someone can read: passing `len(artifacts)` at the call site, or dropping
    the parameter, leaves the constant sitting there documenting a bound that is not
    applied.
    """
    import inspect

    signature = inspect.signature(REMOTE.publish_exact_set)
    assert signature.parameters["concurrency"].default == REMOTE.PUBLISH_CONCURRENCY
    verify_signature = inspect.signature(REMOTE.verify_whole_slice_once)
    assert verify_signature.parameters["concurrency"].default == REMOTE.PUBLISH_CONCURRENCY
    assert 1 < REMOTE.PUBLISH_CONCURRENCY <= R2.DEFAULT_MAX_POOL_CONNECTIONS
    # And `cmd_finalize` must not override it back to serial.
    source = inspect.getsource(HOSTED.cmd_finalize)
    assert "concurrency=" not in source
    # `_run_bounded` refuses a nonsense bound rather than falling back to unbounded.
    with pytest.raises(ValueError, match="concurrency"):
        REMOTE._run_bounded(lambda _i, _x: None, [1, 2], concurrency=0)


def test_the_budget_counter_is_mutated_under_its_lock():
    """The caps are fail-closed limits, charged from PUBLISH_CONCURRENCY threads.

    `self.operations += n` is a read-modify-write and is atomic by NO language
    guarantee. On today's CPython the specialising eval loop happens not to offer a
    thread switch between LOAD_ATTR / BINARY_OP / STORE_ATTR for an int attribute, so
    a counting test cannot distinguish a locked counter from an unlocked one -- it
    passes either way, which is worse than having no test. That accident is not
    something to rely on: it is an implementation detail of one interpreter version,
    and a free-threaded build (PEP 703) removes it outright. A lost update here means
    publishing past the cap #173 gates at plan time.

    So assert the PROPERTY directly -- every charge is taken under the lock -- with an
    instrumented lock. Removing `with self.lock:` fails this immediately, on every
    interpreter, rather than probabilistically on some future one.
    """
    budget = REMOTE.Budget(
        max_operations=10**9, max_write_bytes=10**9, max_read_bytes=10**9
    )
    acquisitions = [0]
    held_during_mutation = []
    real_lock = budget.lock

    class _InstrumentedLock:
        def __enter__(self):
            acquisitions[0] += 1
            real_lock.acquire()
            return self

        def __exit__(self, *exception):
            held_during_mutation.append(budget.operations)
            real_lock.release()
            return False

    budget.lock = _InstrumentedLock()
    charges = 500
    workers = 8

    def _charge():
        for _ in range(charges):
            budget.charge(operations=1, write_bytes=1, read_bytes=1)

    threads = [threading.Thread(target=_charge) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # One acquisition per charge: none of them skipped the lock.
    assert acquisitions[0] == charges * workers
    # And the mutation really happened inside it -- the counter advanced by exactly one
    # per acquisition, observed at release.
    assert sorted(held_during_mutation) == list(range(1, charges * workers + 1))
    assert budget.operations == charges * workers
    assert budget.write_bytes == charges * workers
    assert budget.read_bytes == charges * workers


def test_the_staging_counters_are_safe_under_the_publisher_pool(tmp_path):
    """Same argument, for the counters a fail-closed workflow gate reads.

    Both slice-smoke jobs and the hosted finalize job assert
    `staged_peak_resident_bytes < staged_bytes_hydrated` and
    `staged_objects_released > 0`. Finalize's upload pass drives `path()` and
    `release()` from PUBLISH_CONCURRENCY threads, so a lost update there softens the
    bound the gate is checking.

    Asserted the same way as the Budget lock and for the same reason: a counting test
    cannot tell a locked int counter from an unlocked one on today's CPython, so
    instrument the lock and require every accounting step to take it.
    """
    staging = _load("r2_publication_staging", "scripts/construction_staging_v1.py")
    store = staging.StagedObjectStore(
        type("_Local", (), {"root": tmp_path, "path": lambda self, key: tmp_path / key})(),
        R2.FilesystemStore(tmp_path / "staging"),
        staging.staging_prefix("9" * 64, "places"),
    )
    rounds = 500
    workers = 8
    acquisitions = [0]
    real_lock = store._lock

    class _InstrumentedLock:
        def __enter__(self):
            acquisitions[0] += 1
            real_lock.acquire()
            return self

        def __exit__(self, *exception):
            real_lock.release()
            return False

    store._lock = _InstrumentedLock()

    def _account():
        for _ in range(rounds):
            store._account_hydrated(10)
            store._account_released(10)

    threads = [threading.Thread(target=_account) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    evidence = store.evidence()
    assert acquisitions[0] == 2 * rounds * workers
    assert evidence["staged_objects_hydrated"] == rounds * workers
    assert evidence["staged_objects_released"] == rounds * workers
    assert evidence["staged_bytes_hydrated"] == 10 * rounds * workers
    assert store.resident_bytes == 0


def test_the_shell_mirror_is_gone_from_the_workflow():
    """The blocker itself, asserted absent.

    12.4 hours of aws-cli process startup for a planet address slice against a
    360-minute job timeout. A re-introduced loop would pass every behavioural test in
    the repo, because the Python publisher would still be correct -- it would just
    also be redundant and fatal.
    """
    workflow = (ROOT / ".github/workflows/construction-v1.yml").read_text()
    assert "find publish -type f" not in workflow
    assert "aws s3api put-object" not in workflow
    assert "head-object" not in workflow
    # And no local publication tree is created for finalize to fill.
    assert "mkdir -p final-work publish store" not in workflow
    assert "--remote-root publish" not in workflow


def test_the_operation_projection_moved_with_the_paginated_listing():
    """#173's budget has to price what the real backend charges.

    A listing of N objects is `ceil(N / LIST_PAGE_KEYS)` requests, not one. The
    projection, the fixed terms and the CAPS comment all had to move together; this
    pins that they agree with the primitives rather than with each other.
    """
    control = _load("r2_publication_control", "scripts/construction_v1_control.py")
    for objects in (1, 1000, 1001, 44_305, 65_751, 86_523):
        assert HOSTED.finalize_remote_operations(objects) == (
            objects * 4 + 3 + REMOTE.listing_operations(objects)
        )
    assert HOSTED.FINALIZE_LISTING_PAGE_KEYS == REMOTE.LIST_PAGE_KEYS == R2.LIST_PAGE_KEYS
    # The admitted cap still clears the retry-inclusive structural ceiling AFTER the
    # extra listing pages, which is the only thing that made this safe to change.
    ceiling = HOSTED._finalize_publication_projection(
        "places", partitions=16_888,
        per_record_objects=HOSTED.PER_RECORD_OBJECTS_PER_PACK * 128 * 256,
        basis="structural ceiling",
    )["projected_remote_operations"]
    assert ceiling == 346_182
    assert control.CAPS["max_remote_operations"] >= ceiling
    # And the CAPS comment states the same arithmetic rather than the old one.
    source = (ROOT / "scripts/construction_v1_control.py").read_text()
    assert "ops = 4N + 3 + ceil(N / 1000)" in source
    assert "346,182" in source
    assert "346,096" not in source
