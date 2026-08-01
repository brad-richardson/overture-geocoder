from __future__ import annotations

import io
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "r2_verified_store.py"
SPEC = importlib.util.spec_from_file_location("r2_verified_store", SCRIPT)
assert SPEC and SPEC.loader
shuffle = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = shuffle
SPEC.loader.exec_module(shuffle)


def test_boto3_copy_client_scopes_long_timeout_and_disables_replay(monkeypatch):
    pytest.importorskip("boto3")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")

    store = shuffle.Boto3Store(
        "bucket",
        "https://example.invalid",
        copy_read_timeout_seconds=15 * 60,
    )
    try:
        normal = store.client.meta.config
        copy = store.copy_client.meta.config
        assert normal.read_timeout == 60
        assert normal.retries["total_max_attempts"] == shuffle.MAX_ATTEMPTS + 1
        assert copy.read_timeout == 15 * 60
        assert copy.retries["total_max_attempts"] == 1
        assert store.copy_client is not store.client
    finally:
        store.client.close()
        store.copy_client.close()


@pytest.mark.parametrize("timeout", [0, -1, True, 1.5, "900"])
def test_boto3_copy_client_rejects_invalid_timeout(monkeypatch, timeout):
    pytest.importorskip("boto3")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    with pytest.raises(ValueError, match="positive integer"):
        shuffle.Boto3Store(
            "bucket",
            "https://example.invalid",
            copy_read_timeout_seconds=timeout,
        )


def test_resume_verifies_existing_remote_object_without_overwrite(tmp_path):
    source = tmp_path / "fragment.bin"
    source.write_bytes(b"complete fragment")
    store = shuffle.FilesystemStore(tmp_path / "remote")
    identity = shuffle.artifact_identity(source)
    key = shuffle.immutable_key("runs/1", identity)

    first = shuffle.ensure_uploaded(store, source, key)
    second = shuffle.ensure_uploaded(store, source, key)

    assert first["status"] == "uploaded"
    assert second["status"] == "existing_verified"
    assert second["readback_verified"] is True


def test_existing_corrupt_remote_object_is_never_overwritten(tmp_path):
    source = tmp_path / "fragment.bin"
    source.write_bytes(b"expected")
    store = shuffle.FilesystemStore(tmp_path / "remote")
    identity = shuffle.artifact_identity(source)
    key = shuffle.immutable_key("runs/1", identity)
    store.upload(source, key, identity["sha256"])
    store._path(key).write_bytes(b"corrupt!")

    with pytest.raises(ValueError, match="stored object identity differs"):
        shuffle.ensure_uploaded(store, source, key)
    assert store._path(key).read_bytes() == b"corrupt!"


def test_raced_remote_object_is_never_overwritten(tmp_path):
    source = tmp_path / "fragment.bin"
    source.write_bytes(b"expected")

    class RaceStore(shuffle.FilesystemStore):
        def __init__(self, root):
            super().__init__(root)
            self.first_head = True

        def head_proof(self, key):
            if self.first_head:
                self.first_head = False
                return None
            return super().head_proof(key)

        def upload_fileobj(self, source, key, sha256, *, size):
            raced = tmp_path / "raced.bin"
            raced.write_bytes(b"raced object")
            identity = shuffle.artifact_identity(raced)
            with raced.open("rb") as raced_handle:
                shuffle.FilesystemStore.upload_fileobj(
                    self,
                    raced_handle,
                    key,
                    identity["sha256"],
                    size=identity["bytes"],
                )
            raise FileExistsError(key)

    store = RaceStore(tmp_path / "remote")
    identity = shuffle.artifact_identity(source)
    key = shuffle.immutable_key("runs/1", identity)

    with pytest.raises(ValueError, match="refusing overwrite"):
        shuffle.ensure_uploaded(store, source, key)
    assert store._path(key).read_bytes() == b"raced object"


def test_ensure_uploaded_uses_only_byte_proving_heads(tmp_path):
    source = tmp_path / "fragment.bin"
    source.write_bytes(b"expected")

    class CountingStore(shuffle.FilesystemStore):
        def __init__(self, root):
            super().__init__(root)
            self.proofs = 0
            self.downloads = 0

        def head_proof(self, key):
            self.proofs += 1
            return super().head_proof(key)

        def download_with_info(self, key, destination):
            self.downloads += 1
            return super().download_with_info(key, destination)

    store = CountingStore(tmp_path / "remote")
    identity = shuffle.artifact_identity(source)
    key = shuffle.immutable_key("runs/1", identity)

    assert shuffle.ensure_uploaded(store, source, key)["status"] == "uploaded"
    assert (store.proofs, store.downloads) == (2, 0)

    assert shuffle.ensure_uploaded(store, source, key)["status"] == "existing_verified"
    assert (store.proofs, store.downloads) == (3, 0)


def test_s3_upload_uses_create_only_precondition(tmp_path, monkeypatch):
    source = tmp_path / "fragment.bin"
    source.write_bytes(b"expected")
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(shuffle.subprocess, "run", run)
    shuffle.S3Store("bucket", "https://example.invalid").upload(
        source, "prefix/key", shuffle.sha256_file(source)
    )

    assert ["--if-none-match", "*"] == calls[0][0][
        calls[0][0].index("--if-none-match") : calls[0][0].index("--if-none-match") + 2
    ]


def test_s3_upload_maps_precondition_failure_to_race(tmp_path, monkeypatch):
    source = tmp_path / "fragment.bin"
    source.write_bytes(b"expected")

    def run(command, **kwargs):
        return subprocess.CompletedProcess(
            command, 1, stdout="", stderr="PreconditionFailed (412)"
        )

    monkeypatch.setattr(shuffle.subprocess, "run", run)
    with pytest.raises(FileExistsError, match="appeared"):
        shuffle.S3Store("bucket", "https://example.invalid").upload(
            source, "prefix/key", shuffle.sha256_file(source)
        )


def test_s3_download_maps_only_definitive_absence(tmp_path, monkeypatch):
    store = shuffle.S3Store("bucket", "https://example.invalid")

    def absent(*_args, **_kwargs):
        raise subprocess.CalledProcessError(
            255, ["aws"], output="", stderr="NoSuchKey (404)"
        )

    monkeypatch.setattr(store, "_run", absent)
    with pytest.raises(FileNotFoundError):
        store.download_with_info("missing", tmp_path / "missing")

    def transport_error(*_args, **_kwargs):
        raise subprocess.CalledProcessError(
            255, ["aws"], output="", stderr="connection reset"
        )

    monkeypatch.setattr(store, "_run", transport_error)
    with pytest.raises(RuntimeError, match="connection reset"):
        store.download_with_info("broken", tmp_path / "broken")


def test_boto3_download_retries_a_mid_body_timeout(tmp_path, monkeypatch):
    botocore = pytest.importorskip("botocore.exceptions")

    class TimedOutBody(io.BytesIO):
        def read(self, *_args, **_kwargs):
            raise botocore.ReadTimeoutError(
                endpoint_url="https://example.invalid",
                error=TimeoutError("read timed out"),
            )

    class Client:
        def __init__(self):
            self.calls = 0

        def get_object(self, **_kwargs):
            self.calls += 1
            body = TimedOutBody(b"partial") if self.calls == 1 else io.BytesIO(b"whole")
            return {
                "ContentLength": 5,
                "Metadata": {"sha256": "a" * 64},
                "Body": body,
            }

    store = shuffle.Boto3Store.__new__(shuffle.Boto3Store)
    store.client = Client()
    store.bucket = "bucket"
    store._client_error = type("NeverClientError", (Exception,), {})
    store._stream_retry_error = botocore.HTTPClientError
    monkeypatch.setattr(shuffle.time, "sleep", lambda _seconds: None)

    destination = tmp_path / "download"
    assert store.download_with_info("key", destination) == shuffle.ObjectInfo(
        5, "a" * 64
    )
    assert destination.read_bytes() == b"whole"
    assert store.client.calls == 2


def test_s3_list_prefix_paginates_and_sorts_exact_keys(monkeypatch):
    calls = []
    pages = [
        {
            "IsTruncated": True,
            "Contents": [{"Key": "prefix/b"}],
            "NextContinuationToken": "next-token",
        },
        {
            "IsTruncated": False,
            "Contents": [{"Key": "prefix/a"}],
        },
    ]

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(
            command, 0, stdout=json.dumps(pages.pop(0)), stderr=""
        )

    monkeypatch.setattr(shuffle.subprocess, "run", run)
    store = shuffle.S3Store("bucket", "https://example.invalid")
    assert store.list_prefix("prefix/") == ["prefix/a", "prefix/b"]
    assert "--continuation-token" not in calls[0]
    assert calls[1][calls[1].index("--continuation-token") + 1] == "next-token"
    assert all("--no-paginate" in command for command in calls)


@pytest.mark.parametrize(
    ("pages", "message"),
    [
        (
            [{"IsTruncated": False, "Contents": [{"Key": "elsewhere/key"}]}],
            "escaped",
        ),
        (
            [
                {
                    "IsTruncated": True,
                    "Contents": [{"Key": "prefix/a"}],
                    "NextContinuationToken": "again",
                },
                {
                    "IsTruncated": False,
                    "Contents": [{"Key": "prefix/a"}],
                },
            ],
            "duplicate",
        ),
        (
            [{"IsTruncated": True, "Contents": [{"Key": "prefix/a"}]}],
            "continuation token",
        ),
    ],
)
def test_s3_list_prefix_rejects_stray_duplicate_and_missing_token(
    pages, message, monkeypatch
):
    remaining = list(pages)

    def run(command, **kwargs):
        return subprocess.CompletedProcess(
            command, 0, stdout=json.dumps(remaining.pop(0)), stderr=""
        )

    monkeypatch.setattr(shuffle.subprocess, "run", run)
    with pytest.raises(ValueError, match=message):
        shuffle.S3Store("bucket", "https://example.invalid").list_prefix("prefix/")

def test_stale_local_download_is_replaced_only_after_remote_verification(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"remote truth")
    store = shuffle.FilesystemStore(tmp_path / "remote")
    identity = shuffle.artifact_identity(source)
    key = shuffle.immutable_key("runs/1", identity)
    store.upload(source, key, identity["sha256"])
    destination = tmp_path / "restored.bin"
    destination.write_bytes(b"stale")

    status = shuffle.verified_download(
        store,
        key,
        destination,
        expected_bytes=identity["bytes"],
        expected_sha256=identity["sha256"],
    )

    assert status == "remote_verified"
    assert destination.read_bytes() == b"remote truth"


def test_bad_readback_does_not_replace_existing_local_file(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"expected")
    store = shuffle.FilesystemStore(tmp_path / "remote")
    identity = shuffle.artifact_identity(source)
    key = shuffle.immutable_key("runs/1", identity)
    store.upload(source, key, identity["sha256"])
    store._path(key).write_bytes(b"corrupt!")
    destination = tmp_path / "restored.bin"
    destination.write_bytes(b"keep me")

    with pytest.raises(ValueError, match="SHA-256 differs"):
        shuffle.verified_download(
            store,
            key,
            destination,
            expected_bytes=identity["bytes"],
            expected_sha256=identity["sha256"],
        )
    assert destination.read_bytes() == b"keep me"


def test_manifest_rejects_changed_local_artifact(tmp_path):
    source = tmp_path / "fragment.bin"
    source.write_bytes(b"first")
    manifest = shuffle.build_manifest([source], "runs/1")
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    source.write_bytes(b"second")

    with pytest.raises(ValueError, match="differs from manifest"):
        shuffle.upload_manifest(
            shuffle.FilesystemStore(tmp_path / "remote"), path, "runs/1"
        )


def test_restore_manifest_recovers_from_empty_and_stale_local_state(tmp_path):
    source = tmp_path / "fragment.bin"
    source.write_bytes(b"verified fragment")
    manifest = shuffle.build_manifest([source], "runs/1")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    store = shuffle.FilesystemStore(tmp_path / "remote")
    shuffle.upload_manifest(store, manifest_path, "runs/1")
    output = tmp_path / "restore"

    first = shuffle.restore_manifest(store, manifest_path, output)
    assert first["artifacts"][0]["status"] == "remote_verified"
    restored = output / source.name
    restored.write_bytes(b"stale")
    second = shuffle.restore_manifest(store, manifest_path, output)

    assert second["artifacts"][0]["status"] == "remote_verified"
    assert restored.read_bytes() == b"verified fragment"


def test_upload_rejects_manifest_key_outside_content_identity(tmp_path):
    source = tmp_path / "fragment.bin"
    source.write_bytes(b"fragment")
    manifest = shuffle.build_manifest([source], "runs/1")
    manifest["artifacts"][0]["key"] = "production/catalog.json"
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))

    with pytest.raises(ValueError, match="immutable identity"):
        shuffle.upload_manifest(
            shuffle.FilesystemStore(tmp_path / "remote"), path, "runs/1"
        )


def _copy_store(monkeypatch, client):
    """A Boto3Store wired for copy_within_bucket only, with sleep disabled."""
    botocore = pytest.importorskip("botocore.exceptions")
    store = shuffle.Boto3Store.__new__(shuffle.Boto3Store)
    store.bucket = "bucket"
    store.copy_client = client
    store._client_error = botocore.ClientError
    monkeypatch.setattr(shuffle.time, "sleep", lambda _seconds: None)
    return store


def _client_error(status, code):
    botocore = pytest.importorskip("botocore.exceptions")
    return botocore.ClientError(
        {
            "Error": {"Code": code, "Message": "synthetic"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "CopyObject",
    )


def test_copy_retries_a_definite_internal_error(monkeypatch):
    """Run 30629402228 lost 4h17m to exactly this: a parsed 500 InternalError
    on the Addresses copy, after Places had fully succeeded."""

    class Client:
        def __init__(self):
            self.calls = 0

        def copy_object(self, **_kwargs):
            self.calls += 1
            if self.calls < 3:
                raise _client_error(500, "InternalError")
            return {}

    store = _copy_store(monkeypatch, Client())
    store.copy_within_bucket("source", "destination")
    assert store.copy_client.calls == 3


def test_copy_does_not_retry_an_ambiguous_timeout(monkeypatch):
    """PR #198's rule survives: a read timeout may have LANDED the write, so it
    must not be replayed inside one process. It is not a ClientError, so it
    propagates on the first occurrence."""
    botocore = pytest.importorskip("botocore.exceptions")

    class Client:
        def __init__(self):
            self.calls = 0

        def copy_object(self, **_kwargs):
            self.calls += 1
            raise botocore.ReadTimeoutError(
                endpoint_url="https://example.invalid",
                error=TimeoutError("read timed out"),
            )

    store = _copy_store(monkeypatch, Client())
    with pytest.raises(botocore.ReadTimeoutError):
        store.copy_within_bucket("source", "destination")
    assert store.copy_client.calls == 1, "an ambiguous failure must not replay"


def test_copy_does_not_retry_a_client_side_4xx(monkeypatch):
    """A 4xx is the server refusing, not failing. Retrying cannot help and
    would multiply the cost of a genuinely broken promotion."""

    class Client:
        def __init__(self):
            self.calls = 0

        def copy_object(self, **_kwargs):
            self.calls += 1
            raise _client_error(403, "AccessDenied")

    store = _copy_store(monkeypatch, Client())
    with pytest.raises(Exception, match="AccessDenied"):
        store.copy_within_bucket("source", "destination")
    assert store.copy_client.calls == 1


def test_copy_gives_up_after_a_bounded_number_of_definite_errors(monkeypatch):
    class Client:
        def __init__(self):
            self.calls = 0

        def copy_object(self, **_kwargs):
            self.calls += 1
            raise _client_error(503, "SlowDown")

    store = _copy_store(monkeypatch, Client())
    with pytest.raises(RuntimeError, match="copy-object failed after"):
        store.copy_within_bucket("source", "destination")
    assert store.copy_client.calls == shuffle.COPY_MAX_ATTEMPTS
