import importlib.util
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("construction_v1_remote", ROOT / "scripts/construction_v1_remote.py")
REMOTE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = REMOTE
SPEC.loader.exec_module(REMOTE)


def backend(tmp_path, **overrides):
    caps = {"max_operations": 100, "max_write_bytes": 10_000, "max_read_bytes": 10_000}
    caps.update(overrides)
    return REMOTE.FilesystemRemote(tmp_path / "remote", REMOTE.Budget(**caps))


def test_interruption_resume_marker_last_and_exact_final_stream(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    one = source / "one"
    two = source / "two"
    one.write_bytes(b"one")
    two.write_bytes(b"two")
    remote = backend(tmp_path)
    artifacts = [("construction-v1/binding/slice/a/one", one), ("construction-v1/binding/slice/a/two", two)]
    marker_key = "construction-v1/binding/markers/map-a.json"
    with pytest.raises(RuntimeError, match="injected interruption"):
        REMOTE.publish_exact_set(remote, artifacts=artifacts, marker_key=marker_key, request_sha256="a" * 64, fail_after_upload=1)
    assert remote.head(marker_key) is None
    marker = REMOTE.publish_exact_set(remote, artifacts=artifacts, marker_key=marker_key, request_sha256="a" * 64)
    expected = marker["artifacts"]
    result = REMOTE.verify_whole_slice_once(remote, prefix="construction-v1/binding/slice/a", expected=expected)
    assert result["objects"] == 2
    assert result["bytes"] == 6


def test_conflicting_retry_is_fatal(tmp_path):
    source = tmp_path / "source"
    source.write_bytes(b"wanted")
    remote = backend(tmp_path)
    key = "construction-v1/binding/slice/a/object"
    remote.put_create_only(key, b"wrong")
    with pytest.raises(RuntimeError, match="conflicting immutable object"):
        REMOTE.publish_exact_set(remote, artifacts=[(key, source)], marker_key="construction-v1/binding/markers/a", request_sha256="b" * 64)


def test_operation_and_byte_caps_fail_before_unbounded_work(tmp_path):
    source = tmp_path / "source"
    source.write_bytes(b"too large")
    remote = backend(tmp_path, max_write_bytes=3)
    with pytest.raises(RuntimeError, match="write-byte cap"):
        REMOTE.publish_exact_set(remote, artifacts=[("construction-v1/binding/slice/a/x", source)], marker_key="construction-v1/binding/markers/a", request_sha256="c" * 64)


def test_cleanup_is_exact_preview_only_and_bounded(tmp_path):
    remote = backend(tmp_path)
    preview = "construction-v1/binding/preview/slice-a/"
    remote.put_create_only(preview + "one", b"1")
    remote.put_create_only("construction-v1/binding/slice/slice-a/keep", b"2")
    result = remote.delete_exact([preview + "one"], allowed_prefix=preview, max_objects=1, max_bytes=1)
    assert result == {"objects": 1, "bytes": 1}
    assert remote.head("construction-v1/binding/slice/slice-a/keep") == {"bytes": 1}
    with pytest.raises(RuntimeError, match="cleanup scope"):
        remote.delete_exact(["construction-v1/binding/slice/slice-a/keep"], allowed_prefix=preview, max_objects=1, max_bytes=1)
