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


def test_a_plain_tuple_member_whose_file_changes_between_reads_is_refused(tmp_path):
    """Attack shape 1: `local_member` is digest-verified on NEITHER read.

    Splitting one `read_bytes()` into an admission hash and an upload read lost the
    invariant "the identity and the payload are the same bytes", which used to hold
    by construction. A plain `(key, path)` tuple -- which is what finalize's two
    MANIFESTS are -- goes through no content-addressed store and no digest check, so
    nothing but this re-hash stands between a changed file and a marker that records
    the ADMITTED identity over the UPLOADED bytes.
    """
    source = tmp_path / "manifest.json"
    source.write_bytes(b'{"schema":"admitted-v1"}')
    key = "construction-v1/binding/slice/a/manifest.json"
    remote = backend(tmp_path)

    def swap(_key: str, _identity: dict[str, object]) -> None:
        # Rewrite the file after its identity is admitted, before it is uploaded.
        # SAME LENGTH deliberately, so nothing else in the publisher notices: the
        # per-upload HEAD is a length comparison, and a tuple member is digest-checked
        # on neither read. Without the payload re-hash this publishes silently.
        swapped = b'{"schema":"swapped!!!!"}'
        assert len(swapped) == len(source.read_bytes())
        source.write_bytes(swapped)

    with pytest.raises(RuntimeError, match="payload changed between admission and upload"):
        REMOTE.publish_exact_set(
            remote,
            artifacts=[(key, source)],
            marker_key="construction-v1/binding/markers/tuple.json",
            request_sha256="1" * 64,
            verify=swap,
        )
    # Nothing published and no marker: the swap is caught before the PUT.
    assert remote.head(key) is None
    assert remote.head("construction-v1/binding/markers/tuple.json") is None


def test_a_same_length_swap_is_refused_even_though_head_would_pass(tmp_path):
    """Attack shape 3: the per-upload HEAD compares only `bytes`.

    `head != {"bytes": item["bytes"]}` is a LENGTH check, so a same-length
    substitution satisfies it completely. Demonstrated by asserting the HEAD the
    publisher would have performed does in fact pass on the swapped bytes -- the
    only thing that rejects them is the payload re-hash.
    """
    source = tmp_path / "object"
    admitted_bytes = b"A" * 4096
    swapped_bytes = b"B" * 4096  # identical length, different bytes
    source.write_bytes(admitted_bytes)
    key = "construction-v1/binding/slice/a/same-length"
    remote = backend(tmp_path, max_write_bytes=64 * 1024, max_read_bytes=64 * 1024)

    with pytest.raises(RuntimeError, match="payload changed between admission and upload"):
        REMOTE.publish_exact_set(
            remote,
            artifacts=[(key, source)],
            marker_key="construction-v1/binding/markers/same-length.json",
            request_sha256="2" * 64,
            verify=lambda _k, _i: source.write_bytes(swapped_bytes),
        )
    assert remote.head(key) is None
    # The HEAD check alone would NOT have caught it: same length, so the comparison
    # the publisher performs after each upload is satisfied by the wrong bytes.
    control = backend(tmp_path / "control")
    control.put_create_only(key, swapped_bytes)
    assert control.head(key) == {"bytes": len(admitted_bytes)}


def test_a_staged_member_with_a_refilled_cache_slot_is_refused(tmp_path):
    """Attack shape 2: the real `StagedObjectStore`, finalize's exact Member shape.

    `path()` verifies what it HYDRATES, but returns early on
    `if path.is_file(): return path` -- so the upload loop's re-hydration of an
    object that is already in the local cache is NOT digest-checked. `release()`
    then unlinks that slot, which opens a window in which the cache path is
    unverified-writable and did not exist before the two-pass split. Whatever
    refills it is what gets uploaded.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "remote_test_staging", ROOT / "scripts/construction_staging_v1.py"
    )
    staging_module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = staging_module
    spec.loader.exec_module(staging_module)

    local_root = tmp_path / "local"
    admitted_bytes = b"the bytes the producing phase made" * 64
    digest = hashlib.sha256(admitted_bytes).hexdigest()
    store_key = f"serve/places-v1/objects/sha256/{digest}.plrv"
    source = tmp_path / "produced"
    source.write_bytes(admitted_bytes)

    class _Local:
        """Minimal `LocalObjectStore` surface: no `release`, deterministic paths."""

        def __init__(self, root: Path):
            self.root = root

        def path(self, key: str) -> Path:
            return self.root / key

    backing = staging_module.R2.FilesystemStore(tmp_path / "staging-tree")
    store = staging_module.StagedObjectStore(
        _Local(local_root),
        backing,
        staging_module.staging_prefix("3" * 64, "places"),
    )
    # Seed staging with the real object, exactly as the producing phase would.
    destination = local_root / store_key
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(admitted_bytes)
    staging_module.R2.ensure_uploaded(backing, destination, store.staging_key(store_key))
    destination.unlink()  # finalize starts from an EMPTY local cache

    published_key = f"construction-v1/binding/slice/a/{digest}.plrv"
    swapped = b"X" * len(admitted_bytes)  # same length, so HEAD would pass
    releases: list[int] = []

    def release_then_something_refills_the_slot() -> None:
        """The window itself: released, therefore unverified-writable.

        `release()` unlinks the cache slot; until the next `path()` runs there is a
        path on disk that the store will hand back WITHOUT verifying (because
        `path()` short-circuits on `is_file()`). Refilling it right after admission's
        release is exactly that window -- it did not exist before this function was
        split into two passes, since there was only ever one read.
        """
        store.release(store_key)
        releases.append(1)
        if len(releases) == 1:
            local = local_root / store_key
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(swapped)

    # Finalize's exact construction (construction_v1_hosted.py): hydrate through
    # `store.path`, release through the store's own `release`.
    member = REMOTE.Member(
        key=published_key,
        hydrate=lambda: store.path(store_key),
        release=release_then_something_refills_the_slot,
    )

    remote = backend(tmp_path, max_write_bytes=1 << 20, max_read_bytes=1 << 20)
    with pytest.raises(RuntimeError, match="payload changed between admission and upload"):
        REMOTE.publish_exact_set(
            remote,
            artifacts=[member],
            marker_key="construction-v1/binding/markers/staged.json",
            request_sha256="3" * 64,
        )
    # The wrong bytes reached neither the published tree nor a marker.
    assert remote.head(published_key) is None
    assert remote.head("construction-v1/binding/markers/staged.json") is None
    # And the short-circuit really is the hole being closed: `path()` handed back the
    # unverified refilled file rather than re-fetching and re-verifying it.
    assert (local_root / store_key).read_bytes() == swapped
    assert store.path(store_key).read_bytes() == swapped


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
