"""The publication path against a REAL boto3 client, over a real socket.

WHY THIS FILE EXISTS. Every other test of this path builds its store with
`Boto3Store.__new__` and a hand-written stub whose `put_object` does `Body.read()`
and never seeks. A real botocore client does much more than read: it probes the body
with `seek(0, 2)` to determine content length, may wrap it in `AwsChunkedWrapper`,
signs it, and rewinds it to retry. The first version of this backend could not upload
a single non-empty object over an https endpoint because of exactly that -- and 1241
tests passed, because no test constructed a client. One of them even asserted the
offending raise as desired behaviour.

So: a real client, a real HTTP S3 stand-in, real sockets, and the production
selector (`_publication_remote` -> `s3_object_store` -> `Boto3Store`). No credentials
and no bucket -- the stand-in is a `ThreadingHTTPServer` in this process.

What a local stand-in CANNOT settle, and what is therefore still the owner's
one-object live probe before a planet dispatch: whether R2 honours
`If-None-Match: '*'`, and whether the ETag R2 returns is the content MD5 under
whatever framing the SDK ends up sending. This file pins what the SDK sends and that
the code handles a correct server; it cannot pin R2's half of the contract.
"""

from __future__ import annotations

import hashlib
import http.server
import importlib.util
import sys
import threading
import xml.etree.ElementTree as ElementTree
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytest.importorskip(
    "boto3",
    reason="boto3 is hash-pinned in .github/requirements-hosted-rowgroup.txt; "
    "test_the_persistent_client_is_pinned_for_hosted_runs asserts the pin so a "
    "missing local install cannot quietly drop this coverage in CI",
)


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


HOSTED = _load("real_client_hosted", "scripts/construction_v1_hosted.py")
# Taken from HOSTED rather than loaded again, deliberately. `construction_v1_hosted`
# loads these modules itself, so a second `_load` produces a second module object with
# its OWN `ConflictError` class -- and `publish_exact_set`'s `except ConflictError`
# then does not catch what the remote raises. Reusing HOSTED's instances is both
# correct and what a hosted finalize actually runs.
REMOTE = HOSTED.REMOTE
R2 = HOSTED.STAGING.R2

BUCKET = "cv1.test.bucket"  # a dot makes botocore choose PATH-style addressing, so
# the request host stays 127.0.0.1 instead of becoming a
# virtual-hosted subdomain that cannot resolve.
PREFIX = "construction-v1/binding/slice/a/"
MARKER = "construction-v1/binding/markers/finalize.json"


class _S3StandIn(http.server.BaseHTTPRequestHandler):
    """Just enough S3 to exercise create-only publication and ETag verification."""

    objects: dict[str, dict] = {}
    requests: list[tuple[str, str, dict]] = []
    fail_once: set[str] = set()

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # noqa: A003 - silence the default stderr spam
        return

    # -- helpers ------------------------------------------------------------ #
    def _key(self) -> str:
        path = unquote(urlparse(self.path).path).lstrip("/")
        # Path-style: the first segment is the bucket.
        return path[len(BUCKET) + 1 :] if path.startswith(BUCKET + "/") else ""

    def _respond(self, status: int, body: bytes = b"", headers: dict | None = None):
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    # -- verbs -------------------------------------------------------------- #
    def do_PUT(self):
        key = self._key()
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        type(self).requests.append(("PUT", key, dict(self.headers)))
        if key in type(self).fail_once:
            type(self).fail_once.discard(key)
            self._respond(500, b"<Error><Code>InternalError</Code></Error>")
            return
        if self.headers.get("If-None-Match") == "*" and key in type(self).objects:
            self._respond(
                412, b"<Error><Code>PreconditionFailed</Code></Error>"
            )
            return
        metadata = {
            name[len("x-amz-meta-") :].lower(): value
            for name, value in self.headers.items()
            if name.lower().startswith("x-amz-meta-")
        }
        type(self).objects[key] = {"body": body, "metadata": metadata}
        self._respond(200, headers={"ETag": '"%s"' % hashlib.md5(body).hexdigest()})

    def do_HEAD(self):
        key = self._key()
        type(self).requests.append(("HEAD", key, dict(self.headers)))
        item = type(self).objects.get(key)
        if item is None:
            self._respond(404)
            return
        headers = {"ETag": '"%s"' % hashlib.md5(item["body"]).hexdigest()}
        for name, value in item["metadata"].items():
            headers["x-amz-meta-" + name] = value
        self.send_response(200)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(item["body"])))
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if query.get("list-type") == ["2"]:
            self._list(query)
            return
        key = self._key()
        type(self).requests.append(("GET", key, dict(self.headers)))
        item = type(self).objects.get(key)
        if item is None:
            self._respond(404, b"<Error><Code>NoSuchKey</Code></Error>")
            return
        headers = {"ETag": '"%s"' % hashlib.md5(item["body"]).hexdigest()}
        for name, value in item["metadata"].items():
            headers["x-amz-meta-" + name] = value
        self._respond(
            200,
            item["body"],
            headers,
        )

    def _list(self, query):
        prefix = query.get("prefix", [""])[0]
        max_keys = int(query.get("max-keys", ["1000"])[0])
        token = query.get("continuation-token", ["0"])[0]
        keys = sorted(k for k in type(self).objects if k.startswith(prefix))
        start = int(token)
        page = keys[start : start + max_keys]
        end = start + len(page)
        truncated = end < len(keys)
        root = ElementTree.Element(
            "ListBucketResult", xmlns="http://s3.amazonaws.com/doc/2006-03-01/"
        )
        ElementTree.SubElement(root, "IsTruncated").text = str(truncated).lower()
        if truncated:
            ElementTree.SubElement(root, "NextContinuationToken").text = str(end)
        for key in page:
            entry = ElementTree.SubElement(root, "Contents")
            ElementTree.SubElement(entry, "Key").text = key
        self._respond(200, ElementTree.tostring(root), {"Content-Type": "application/xml"})


@pytest.fixture
def s3_stand_in(monkeypatch):
    """A real HTTP S3 stand-in plus the endpoint URL pointing at it."""
    _S3StandIn.objects = {}
    _S3StandIn.requests = []
    _S3StandIn.fail_once = set()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _S3StandIn)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _objects(tmp_path, count=6, size=3 * 1024 * 1024):
    source = tmp_path / "source"
    source.mkdir(parents=True, exist_ok=True)
    artifacts = []
    for index in range(count):
        path = source / f"{index:04d}"
        path.write_bytes(bytes([index % 251]) * size)
        artifacts.append((f"{PREFIX}{index:04d}", path))
    return artifacts


def _remote(endpoint, budget=None):
    import argparse

    budget = budget or REMOTE.Budget(
        max_operations=100_000, max_write_bytes=10**10, max_read_bytes=10**10
    )
    # THE PRODUCTION SELECTOR, not a hand-built backend: this is the code path a
    # hosted finalize takes, flags and all.
    args = argparse.Namespace(
        remote_root=None, remote_bucket=BUCKET, remote_endpoint_url=endpoint
    )
    return HOSTED._publication_remote(args, budget), budget


# --------------------------------------------------------------------------- #
# 1. The P0: a non-empty object really uploads
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("size", [0, 1, 1024, 3 * 1024 * 1024])
def test_a_real_client_publishes_objects_of_every_size(s3_stand_in, tmp_path, size):
    """The regression test for the defect that made this path non-functional.

    A real botocore client probes the request body with `seek(0, 2)`. The first
    version of `_MD5Reader` raised on any non-zero seek, so every PUT of a non-empty
    body died before a byte was sent -- and only the 0-byte case survived, because
    seeking to the end of an empty file lands at 0. `size` is parametrized across that
    boundary deliberately: a suite that only ever published empty or stubbed bodies is
    exactly how this shipped.
    """
    artifacts = _objects(tmp_path, count=3, size=size)
    remote, _ = _remote(s3_stand_in)
    marker = REMOTE.publish_exact_set(
        remote, artifacts=artifacts, marker_key=MARKER, request_sha256="a" * 64
    )
    assert len(marker["artifacts"]) == 3
    # The bytes are really in the stand-in, and they are the right bytes.
    for key, path in artifacts:
        assert _S3StandIn.objects[key]["body"] == path.read_bytes()
    assert MARKER in _S3StandIn.objects
    verification = REMOTE.verify_whole_slice_once(
        remote, prefix=PREFIX, expected=marker["artifacts"]
    )
    assert verification["objects"] == 3
    assert verification["bytes"] == 3 * size


def test_the_request_is_a_plain_single_put_with_no_aws_chunked_framing(s3_stand_in, tmp_path):
    """No flexible-checksum trailer, on the wire, at a non-zero size.

    Two things ride on this and both were broken by botocore's default
    `when_supported`: the trailer path is what probed the body and killed the upload,
    and `aws-chunked` + `x-amz-checksum-crc32` is framing the aws-cli mirror never
    used and R2's acceptance of which is unverified. Rejecting
    `x-amz-checksum-sha256` as unverified while unconditionally sending an unverified
    CRC32 trailer would be incoherent, so assert the wire format rather than trusting
    the Config.
    """
    artifacts = _objects(tmp_path, count=1, size=2 * 1024 * 1024)
    remote, _ = _remote(s3_stand_in)
    REMOTE.publish_exact_set(
        remote, artifacts=artifacts, marker_key=MARKER, request_sha256="b" * 64
    )
    puts = [headers for verb, _key, headers in _S3StandIn.requests if verb == "PUT"]
    assert puts, "nothing was PUT"
    for headers in puts:
        assert headers.get("Content-Encoding") is None, headers.get("Content-Encoding")
        assert headers.get("x-amz-trailer") is None
        assert "x-amz-checksum-crc32" not in {name.lower() for name in headers}
        assert headers.get("If-None-Match") == "*"
        # A real Content-Length, i.e. not a streaming/chunked body.
        assert int(headers["Content-Length"]) > 0


def test_the_etag_the_client_gets_back_is_the_content_md5(s3_stand_in, tmp_path):
    """The whole-slice verification's content proof, end to end through a real client.

    `read_back_identity` compares the store's ETag to the MD5 of the bytes sent. That
    only works if the SDK's framing leaves the ETag as a content MD5, which is the
    other half of what `when_required` buys.
    """
    artifacts = _objects(tmp_path, count=2, size=1_000_003)
    remote, _ = _remote(s3_stand_in)
    marker = REMOTE.publish_exact_set(
        remote, artifacts=artifacts, marker_key=MARKER, request_sha256="c" * 64
    )
    for key, path in artifacts:
        proof = remote.store.head_proof(key)
        assert proof["content_md5"] == hashlib.md5(path.read_bytes()).hexdigest()
        assert proof["bytes"] == path.stat().st_size
    assert REMOTE.verify_whole_slice_once(
        remote, prefix=PREFIX, expected=marker["artifacts"]
    )["objects"] == 2


def test_staging_proofs_avoid_redundant_class_b_requests(s3_stand_in, tmp_path):
    source = tmp_path / "staged.bin"
    source.write_bytes(b"staged bytes")
    identity = R2.artifact_identity(source)
    key = R2.immutable_key("staging/test", identity)
    store = R2.s3_object_store(BUCKET, s3_stand_in)

    assert R2.ensure_uploaded(store, source, key)["status"] == "uploaded"
    assert [verb for verb, _, _ in _S3StandIn.requests] == ["HEAD", "PUT", "HEAD"]

    _S3StandIn.requests.clear()
    assert R2.ensure_uploaded(store, source, key)["status"] == "existing_verified"
    assert [verb for verb, _, _ in _S3StandIn.requests] == ["HEAD"]

    _S3StandIn.requests.clear()
    destination = tmp_path / "hydrated.bin"
    assert (
        R2.verified_content_addressed_download(
            store,
            key,
            destination,
            expected_sha256=identity["sha256"],
        )
        == "remote_verified"
    )
    assert destination.read_bytes() == source.read_bytes()
    assert [verb for verb, _, _ in _S3StandIn.requests] == ["GET"]


def test_a_real_client_retry_resends_the_body_and_the_digest_follows(s3_stand_in, tmp_path):
    """A transient 500 must be survivable, and the digest must describe what arrived.

    This is the property the old `seek` was trying to protect and got backwards. The
    SDK rewinds the body and resends; the recorded MD5 has to be of the bytes that
    actually landed, or the whole-slice comparison fails on a run that published
    correctly.
    """
    artifacts = _objects(tmp_path, count=1, size=512 * 1024)
    key = artifacts[0][0]
    _S3StandIn.fail_once = {key}
    remote, _ = _remote(s3_stand_in)
    marker = REMOTE.publish_exact_set(
        remote, artifacts=artifacts, marker_key=MARKER, request_sha256="d" * 64
    )
    # The retry really happened: two PUTs for one object.
    assert len([1 for verb, k, _h in _S3StandIn.requests if verb == "PUT" and k == key]) == 2
    assert _S3StandIn.objects[key]["body"] == artifacts[0][1].read_bytes()
    assert remote._sent_md5[key] == hashlib.md5(
        artifacts[0][1].read_bytes()
    ).hexdigest()
    assert REMOTE.verify_whole_slice_once(
        remote, prefix=PREFIX, expected=marker["artifacts"]
    )["objects"] == 1


def test_a_length_probe_does_not_disturb_the_digest(tmp_path):
    """`seek(0, 2)` reads nothing, so it must not affect the digest -- or raise.

    This is the exact call `botocore.utils.determine_content_length` makes, and the
    exact call the first version died on.
    """
    source = tmp_path / "body"
    payload = b"payload" * 1000
    source.write_bytes(payload)
    with source.open("rb") as handle:
        reader = REMOTE._MD5Reader(handle)
        assert reader.seek(0, 2) == len(payload)  # probe to the end
        assert reader.tell() == len(payload)
        reader.seek(0)
        assert reader.read() == payload
        assert reader.content_md5(len(payload)) == hashlib.md5(payload).hexdigest()


def test_a_partially_read_body_reports_no_digest_rather_than_a_wrong_one(tmp_path):
    """Fail closed: an unaccountable read gives None, and the caller re-reads.

    None means `read_back_identity` performs the full streaming read-back. The check
    gets slower, never weaker -- which is why refusing to guess here is cheap.
    """
    source = tmp_path / "body"
    payload = b"z" * 4096
    source.write_bytes(payload)
    with source.open("rb") as handle:
        reader = REMOTE._MD5Reader(handle)
        reader.read(100)
        assert reader.content_md5(len(payload)) is None  # incomplete pass
    with source.open("rb") as handle:
        reader = REMOTE._MD5Reader(handle)
        reader.seek(1000)
        reader.read()  # a read that never started at 0
        assert reader.content_md5(len(payload)) is None


def test_create_only_and_the_byte_exact_conflict_hold_through_a_real_client(
    s3_stand_in, tmp_path
):
    """`If-None-Match: '*'` really produces a 412, and the conflict path compares bytes."""
    artifacts = _objects(tmp_path, count=2, size=8192)
    remote, _ = _remote(s3_stand_in)
    marker = REMOTE.publish_exact_set(
        remote, artifacts=artifacts, marker_key=MARKER, request_sha256="e" * 64
    )
    # A byte-identical resume is accepted: every PUT 412s and every conflict verifies.
    resumed, _ = _remote(s3_stand_in)
    assert REMOTE.publish_exact_set(
        resumed, artifacts=artifacts, marker_key=MARKER, request_sha256="e" * 64
    )["artifacts"] == marker["artifacts"]
    # A DIFFERING object under the same key is refused, not accepted on existence.
    victim, path = artifacts[0]
    _S3StandIn.objects[victim] = {
        "body": b"Q" * path.stat().st_size,  # same length, different bytes
        "metadata": {"sha256": "0" * 64},
    }
    another, _ = _remote(s3_stand_in)
    with pytest.raises(RuntimeError, match="conflicting immutable object"):
        REMOTE.publish_exact_set(
            another, artifacts=artifacts, marker_key=MARKER, request_sha256="e" * 64
        )


def test_a_paginated_listing_round_trips_through_a_real_client(s3_stand_in, tmp_path):
    """The shared page validator, against real ListObjectsV2 XML and continuation."""
    artifacts = _objects(tmp_path, count=7, size=64)
    remote, _ = _remote(s3_stand_in)
    marker = REMOTE.publish_exact_set(
        remote, artifacts=artifacts, marker_key=MARKER, request_sha256="f" * 64
    )
    store = R2.s3_object_store(BUCKET, s3_stand_in)
    assert store.list_prefix(PREFIX) == sorted(key for key, _p in artifacts)
    # Force real pagination: three keys per page over seven keys.
    original = R2.LIST_PAGE_KEYS
    try:
        R2.LIST_PAGE_KEYS = 3
        assert R2.s3_object_store(BUCKET, s3_stand_in).list_prefix(PREFIX) == sorted(
            key for key, _p in artifacts
        )
    finally:
        R2.LIST_PAGE_KEYS = original
    assert REMOTE.verify_whole_slice_once(
        remote, prefix=PREFIX, expected=marker["artifacts"]
    )["objects"] == 7


def test_the_persistent_client_is_pinned_for_hosted_runs():
    """boto3 must be in the hash-pinned set, so CI always has this coverage.

    `importorskip` at the top of this file would otherwise let the only real-client
    coverage in the repo vanish silently -- which is the same shape as the defect the
    file exists for.
    """
    requirements = (ROOT / ".github/requirements-hosted-rowgroup.txt").read_text()
    assert "boto3==" in requirements
    assert "botocore==" in requirements
