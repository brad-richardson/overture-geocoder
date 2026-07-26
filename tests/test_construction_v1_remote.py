import hashlib
import importlib.util
import sys
import threading
import time
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


def _many_objects(tmp_path, *, object_bytes=OBJECT_BYTES, count=OBJECT_COUNT):
    """A set whose total is large enough that holding it all is measurable."""
    source = tmp_path / "source"
    source.mkdir(parents=True)
    artifacts = []
    for index in range(count):
        path = source / f"{index:04d}"
        # Distinct bytes per object, so nothing is deduplicated by the allocator or
        # by the create-only backend.
        path.write_bytes(bytes([index]) * object_bytes)
        artifacts.append((f"construction-v1/binding/slice/a/{index:04d}", path))
    return artifacts


def _publish_and_measure_peak(tmp_path, *, object_bytes, count, tag):
    """Publish `count` objects of `object_bytes` and return the traced RAM peak."""
    artifacts = _many_objects(tmp_path / tag, object_bytes=object_bytes, count=count)
    total = count * object_bytes
    remote = REMOTE.FilesystemRemote(
        tmp_path / tag / "remote",
        REMOTE.Budget(max_operations=count * 8, max_write_bytes=total * 2,
                      max_read_bytes=total * 2),
    )
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        marker = REMOTE.publish_exact_set(
            remote,
            artifacts=artifacts,
            marker_key=f"construction-v1/binding/markers/ram-{tag}.json",
            request_sha256="d" * 64,
        )
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    # The whole set really was published, so the bound is not vacuous.
    assert len(marker["artifacts"]) == count
    verified = REMOTE.verify_whole_slice_once(
        remote, prefix="construction-v1/binding/slice/a", expected=marker["artifacts"]
    )
    assert verified["bytes"] == total
    return peak, total


def test_publish_ram_is_the_chunk_times_the_workers_not_the_object_size(tmp_path):
    """RAM does not scale with object size, and does not scale with the set.

    `publish_exact_set` used to read EVERY artifact's full bytes into one dict during
    admission and consume them from that dict in the upload loop; #170 cut that to
    one whole object at a time via `read_bytes()`. Neither survives concurrency:
    16 workers over ~180 MB planet address objects is ~3 GB if a payload is read
    whole. The upload pass therefore STREAMS, so the bound is
    `PUBLISH_CONCURRENCY x CHUNK_BYTES` -- independent of how big the objects are.

    Independence is the assertion, and it needs two sizes to be one: publish the
    same object COUNT at 1x and at 8x the streaming chunk. Peak must stay under the
    chunk-derived ceiling in both cases and must not grow with the object -- a
    publisher that materialises a payload takes 8x more RAM for 8x bigger objects and
    fails on the ratio, which no single-size measurement can see. A test on the
    published SET would not catch it either: the output was always correct, only the
    peak was fatal.
    """
    count = 24
    small_peak, small_total = _publish_and_measure_peak(
        tmp_path, object_bytes=REMOTE.CHUNK_BYTES, count=count, tag="small"
    )
    large_peak, large_total = _publish_and_measure_peak(
        tmp_path, object_bytes=8 * REMOTE.CHUNK_BYTES, count=count, tag="large"
    )
    assert large_total == 8 * small_total
    # One chunk per worker, doubled for allocator slack and the digest's own buffers.
    # Stated in terms of the CHUNK rather than the object, which is the whole claim.
    ceiling = 2 * REMOTE.PUBLISH_CONCURRENCY * REMOTE.CHUNK_BYTES
    assert small_peak < ceiling, f"peak {small_peak}, ceiling {ceiling}"
    assert large_peak < ceiling, f"peak {large_peak}, ceiling {ceiling}"
    # 8x the bytes must not be ~8x the RAM. `read_bytes()` in the upload pass makes
    # this ratio 8; streaming makes it ~2 (bigger objects keep more workers busy at
    # once, which moves the peak without making it depend on the object size).
    assert large_peak < 4 * small_peak, (
        f"peak grew from {small_peak} to {large_peak} for 8x bigger objects, so the "
        "upload pass is materialising payloads rather than streaming them"
    )
    # And still far under the set, which is the bound that tightens as the planet
    # slice grows.
    assert large_peak < large_total // 4


def test_publish_residency_is_bounded_by_the_worker_count_and_admission_holds_one(tmp_path):
    """Bounded local disk: at most PUBLISH_CONCURRENCY objects resident, ever.

    Finalize used to build its exact set as a list of `store.path(...)` results,
    which hydrated every published object from R2 staging onto the runner before the
    first upload and released none of them. #170 cut that to one at a time; the
    upload pass is now a worker pool, so the honest bound is the WORKER COUNT -- and
    it has to be an assertion, because "bounded" and "unbounded" differ only in the
    peak.

    Two separate claims, because they have different bounds:

    * the ADMISSION pass is serial, so residency there is exactly 1. This is the
      pass that fixes the admitted set and runs the caller's fail-closed gates, and
      it is measured inside the `verify` hook -- which is called while the member it
      describes is the only one resident.
    * the UPLOAD pass holds at most `PUBLISH_CONCURRENCY`. With 24 objects and 16
      workers, a publisher that submitted the whole set at once would peak at 24 and
      fail this.
    """
    artifacts = _many_objects(tmp_path)
    lock = threading.Lock()
    resident: set[str] = set()
    peak_resident = [0]
    admission_peak = [0]
    released: list[str] = []

    def member(key: str, path: Path) -> object:
        def hydrate() -> Path:
            with lock:
                resident.add(key)
                peak_resident[0] = max(peak_resident[0], len(resident))
            return path

        def release() -> None:
            with lock:
                resident.discard(key)
                released.append(key)

        return REMOTE.Member(key=key, hydrate=hydrate, release=release)

    def observe_admission(_key, _identity):
        admission_peak[0] = max(admission_peak[0], len(resident))

    total = OBJECT_COUNT * OBJECT_BYTES
    remote = backend(tmp_path, max_write_bytes=total * 2, max_read_bytes=total * 2,
                     max_operations=OBJECT_COUNT * 8)
    marker = REMOTE.publish_exact_set(
        remote,
        artifacts=[member(key, path) for key, path in artifacts],
        marker_key="construction-v1/binding/markers/resident.json",
        request_sha256="e" * 64,
        verify=observe_admission,
    )
    assert admission_peak[0] == 1
    assert 1 <= peak_resident[0] <= REMOTE.PUBLISH_CONCURRENCY
    # Not vacuous: the set is bigger than the bound, so an unbounded publisher would
    # exceed it.
    assert OBJECT_COUNT > REMOTE.PUBLISH_CONCURRENCY
    assert resident == set()
    # Twice each: once to hash it into the admitted set, once to upload it.
    assert len(released) == 2 * OBJECT_COUNT
    assert sorted(set(released)) == sorted(item["key"] for item in marker["artifacts"])


def test_the_upload_pass_really_runs_concurrently(tmp_path):
    """The worker pool is a pool, not a loop with a pool-shaped name.

    Unwiring the concurrency -- `concurrency=1`, or an executor with one worker --
    leaves every other test in this file passing, because a serial publisher is a
    correct publisher. It is just the 12.4-hour one. So assert overlap directly:
    with 16 workers and 24 members, at least two uploads must be in flight at once.
    """
    artifacts = _many_objects(tmp_path)
    lock = threading.Lock()
    in_flight = [0]
    peak_in_flight = [0]

    class _CountingRemote(REMOTE.FilesystemRemote):
        def put_create_only_stream(self, key, source, *, size, sha256):
            with lock:
                in_flight[0] += 1
                peak_in_flight[0] = max(peak_in_flight[0], in_flight[0])
            time.sleep(0.01)
            try:
                return super().put_create_only_stream(
                    key, source, size=size, sha256=sha256
                )
            finally:
                with lock:
                    in_flight[0] -= 1

    total = OBJECT_COUNT * OBJECT_BYTES
    remote = _CountingRemote(
        tmp_path / "remote",
        REMOTE.Budget(max_operations=OBJECT_COUNT * 8, max_write_bytes=total * 2,
                      max_read_bytes=total * 2),
    )
    REMOTE.publish_exact_set(
        remote,
        artifacts=artifacts,
        marker_key="construction-v1/binding/markers/parallel.json",
        request_sha256="7" * 64,
    )
    assert peak_in_flight[0] > 1, "the upload pass ran serially"
    assert peak_in_flight[0] <= REMOTE.PUBLISH_CONCURRENCY


def test_the_admitted_set_is_offered_to_the_remote_in_sorted_order(tmp_path):
    """Submission order is the admitted order, even though completion order is not.

    Concurrency reorders COMPLETIONS -- that is what it is for -- but the publisher
    still walks the admitted, sorted set front to back. Members are built in reverse
    key order here so a publisher that skipped the sort, or that iterated the input
    sequence instead of the admitted one, is visible.
    """
    artifacts = list(reversed(_many_objects(tmp_path)))
    offered: list[str] = []
    lock = threading.Lock()

    class _RecordingRemote(REMOTE.FilesystemRemote):
        def put_create_only_stream(self, key, source, *, size, sha256):
            with lock:
                offered.append(key)
            return super().put_create_only_stream(key, source, size=size, sha256=sha256)

    total = OBJECT_COUNT * OBJECT_BYTES
    remote = _RecordingRemote(
        tmp_path / "remote",
        REMOTE.Budget(max_operations=OBJECT_COUNT * 8, max_write_bytes=total * 2,
                      max_read_bytes=total * 2),
    )
    marker = REMOTE.publish_exact_set(
        remote,
        artifacts=artifacts,
        # One worker: submission order and observation order are then the same, so
        # this test measures ORDER without racing on it. Overlap is asserted
        # separately, above.
        concurrency=1,
        marker_key="construction-v1/binding/markers/order.json",
        request_sha256="8" * 64,
    )
    assert offered == sorted(offered)
    assert offered == [str(item["key"]) for item in marker["artifacts"]]


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
