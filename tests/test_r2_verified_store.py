from __future__ import annotations

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

    with pytest.raises(ValueError, match="SHA-256 differs"):
        shuffle.ensure_uploaded(store, source, key)
    assert store._path(key).read_bytes() == b"corrupt!"


def test_raced_remote_object_is_never_overwritten(tmp_path):
    source = tmp_path / "fragment.bin"
    source.write_bytes(b"expected")

    class RaceStore(shuffle.FilesystemStore):
        def __init__(self, root):
            super().__init__(root)
            self.first_head = True

        def head(self, key):
            if self.first_head:
                self.first_head = False
                return None
            return super().head(key)

        def upload(self, source, key, sha256):
            raced = tmp_path / "raced.bin"
            raced.write_bytes(b"raced object")
            identity = shuffle.artifact_identity(raced)
            super().upload(raced, key, identity["sha256"])
            raise FileExistsError(key)

    store = RaceStore(tmp_path / "remote")
    identity = shuffle.artifact_identity(source)
    key = shuffle.immutable_key("runs/1", identity)

    with pytest.raises(ValueError, match="refusing overwrite"):
        shuffle.ensure_uploaded(store, source, key)
    assert store._path(key).read_bytes() == b"raced object"


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
