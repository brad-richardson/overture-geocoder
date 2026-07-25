import hashlib
import importlib.util
import sys
import tracemalloc
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


OBJECT_BYTES = 512 * 1024
OBJECT_COUNT = 24


def _many_objects(tmp_path):
    """A set whose total is large enough that holding it all is measurable."""
    source = tmp_path / "source"
    source.mkdir()
    artifacts = []
    for index in range(OBJECT_COUNT):
        path = source / f"{index:04d}"
        # Distinct bytes per object, so nothing is deduplicated by the allocator or
        # by the create-only backend.
        path.write_bytes(bytes([index]) * OBJECT_BYTES)
        artifacts.append((f"construction-v1/binding/slice/a/{index:04d}", path))
    return artifacts


def test_publish_holds_one_payload_in_ram_not_the_whole_set(tmp_path):
    """The published set is not resident in RAM all at once.

    `publish_exact_set` used to read EVERY artifact's full bytes into one dict
    during admission and consume them from that dict in the upload loop. On the
    Monaco slice that is 36 MB and invisible; at planet scale it is 13-18 GB (a
    ~10-11 GB head payload plus 3.3-6.7 GB of positions packs) on a 16 GB runner,
    at the very end of a multi-hour run -- an unconditional OOM.

    A test on the published SET would not have caught it: the output was correct,
    only the peak was fatal. So measure the peak. `tracemalloc` sees the `bytes`
    objects the payload reads allocate, which is exactly the thing that used to
    accumulate.
    """
    artifacts = _many_objects(tmp_path)
    total = OBJECT_COUNT * OBJECT_BYTES
    remote = backend(tmp_path, max_write_bytes=total * 2, max_read_bytes=total * 2,
                     max_operations=OBJECT_COUNT * 8)
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        marker = REMOTE.publish_exact_set(
            remote,
            artifacts=artifacts,
            marker_key="construction-v1/binding/markers/ram.json",
            request_sha256="d" * 64,
        )
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    # The whole set really was published, so the bound below is not vacuous.
    assert len(marker["artifacts"]) == OBJECT_COUNT
    verified = REMOTE.verify_whole_slice_once(
        remote, prefix="construction-v1/binding/slice/a", expected=marker["artifacts"]
    )
    assert verified["bytes"] == total
    # Generous by 4x on a single object and still an order of magnitude under the
    # old behaviour, which allocated the entire set (>= `total`) before uploading
    # anything. The assertion is against the SET size, so it tightens automatically
    # as the set grows -- which is the direction the planet run scales.
    assert peak < 4 * OBJECT_BYTES, f"peak {peak} of a {total}-byte set"
    assert peak < total // 2


def test_publish_brackets_every_read_with_hydrate_and_release(tmp_path):
    """One object resident at a time -- the DISK half of the same defect.

    Finalize used to build its exact set as a list of `store.path(...)` results,
    which hydrated every published object from R2 staging onto the runner before
    the first upload and released none of them. A bounded publisher must hold each
    member only while it is reading it, so record residency and assert the maximum
    concurrency is one, and that nothing is left behind.
    """
    artifacts = _many_objects(tmp_path)
    resident: set[str] = set()
    peak_resident: list[int] = [0]
    released: list[str] = []

    def member(key: str, path: Path) -> object:
        def hydrate() -> Path:
            resident.add(key)
            peak_resident[0] = max(peak_resident[0], len(resident))
            return path

        def release() -> None:
            resident.discard(key)
            released.append(key)

        return REMOTE.Member(key=key, hydrate=hydrate, release=release)

    total = OBJECT_COUNT * OBJECT_BYTES
    remote = backend(tmp_path, max_write_bytes=total * 2, max_read_bytes=total * 2,
                     max_operations=OBJECT_COUNT * 8)
    marker = REMOTE.publish_exact_set(
        remote,
        artifacts=[member(key, path) for key, path in artifacts],
        marker_key="construction-v1/binding/markers/resident.json",
        request_sha256="e" * 64,
    )
    assert peak_resident[0] == 1
    assert resident == set()
    # Twice each: once to hash it into the admitted set, once to upload it. Both
    # bracketed, neither overlapping another member.
    assert len(released) == 2 * OBJECT_COUNT
    assert sorted(set(released)) == sorted(item["key"] for item in marker["artifacts"])


def test_admission_verify_hook_runs_before_any_upload(tmp_path):
    """The pre-publication identity gate is a gate, not a report.

    Finalize's content-addressed + provenance checks moved into this hook so an
    object is hashed once per residency instead of hydrated a third time. That is
    only safe if admission still completes before the publisher writes a byte.
    """
    artifacts = _many_objects(tmp_path)[:4]
    seen: list[str] = []

    def verify(key: str, identity: dict[str, object]) -> None:
        assert identity["bytes"] == OBJECT_BYTES
        assert identity["sha256"] == hashlib.sha256(
            Path(dict(artifacts)[key]).read_bytes()
        ).hexdigest()
        seen.append(key)
        if len(seen) == len(artifacts):
            raise RuntimeError("rejected during admission")

    remote = backend(tmp_path, max_write_bytes=10 * OBJECT_BYTES,
                     max_read_bytes=10 * OBJECT_BYTES)
    with pytest.raises(RuntimeError, match="rejected during admission"):
        REMOTE.publish_exact_set(
            remote,
            artifacts=artifacts,
            marker_key="construction-v1/binding/markers/gate.json",
            request_sha256="f" * 64,
            verify=verify,
        )
    assert len(seen) == len(artifacts)
    # Rejecting the LAST member still published nothing: the gate ran on every one
    # of them before the first upload.
    assert remote.list("construction-v1/binding/slice/a") == []
    assert remote.head("construction-v1/binding/markers/gate.json") is None


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
