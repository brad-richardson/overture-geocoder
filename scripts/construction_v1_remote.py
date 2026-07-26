#!/usr/bin/env python3
"""Bounded, create-only remote publication primitives for construction-v1.

Two backends, one publisher. ``FilesystemRemote`` is the reference: it is what
every test and the credential-free slice harness publish into, so the risky logic
is exercised offline. ``VerifiedStoreRemote`` is the real one: it publishes
straight into an ``r2_verified_store.ObjectStore``, which for a hosted run is a
persistent-client R2 client. Hosted credentials belong in the workflow adapter,
never here.

The publisher used to be ``FilesystemRemote`` plus a shell mirror in
``construction-v1.yml``, and that mirror was the top blocker for a planet
dispatch. It was a serial ``find | while read`` loop spending at least two ``aws
s3api`` invocations per object at 0.339 s of CPU startup each -- 12.4 hours for a
65,751-object address slice, 8.4 hours for 44,305 places objects, against a
360-minute job timeout, before a byte moved. It also wrote the whole slice
(~100-145 GB for addresses) to a local tree first. Both are gone: uploads are a
bounded worker pool over a persistent client, and nothing is staged locally that
was not already resident for one read.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import math
import os
import sys
import tempfile
import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterable, Sequence


ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# Reused rather than reimplemented: the create-only + sha256-metadata +
# paginated-listing rules already live there, and a second S3 client would be a
# second place for a fail-closed rule to rot.
R2 = _load("construction_v1_remote_r2_store", "scripts/r2_verified_store.py")

# Keys per listing page, and therefore the class-A operations one exact-prefix
# listing costs. Taken from the store rather than re-typed so the reference
# backend prices the same paginated listing the real one performs -- otherwise the
# budget test that pins `finalize_remote_operations` against the primitives would
# be pinning it against a cost R2 does not charge.
LIST_PAGE_KEYS = R2.LIST_PAGE_KEYS

# The free-disk floor the finalize job asserts before it starts
# (`construction-v1.yml`: `df -Pk / | ... -ge 25000000`, i.e. 25 GB in 1 KiB blocks).
# Named here because PUBLISH_CONCURRENCY is bounded BY it -- a worker count chosen
# without reference to the disk it consumes is a coincidence, not a bound.
FINALIZE_FREE_DISK_FLOOR_BYTES = 25_000_000 * 1024
# S3/R2 single-PUT ceiling. `put_object` is used directly (never the transfer
# manager), so any object above this cannot be published at all -- and
# `single_part_etag_md5` would never get the chance to notice, because the PUT itself
# fails. Asserted against `max_serving_bytes` in the tests.
SINGLE_PUT_MAX_BYTES = 5 * 1000**3

# Workers in the upload pass. Chosen, not guessed:
#
# * The per-object cost with a persistent client is two round trips (the
#   create-only PUT and the per-upload HEAD) and no CPU. At a hosted runner's
#   ~50-150 ms per R2 round trip that is ~0.1-0.3 s of pure latency per object, so
#   16 in flight turns ~4 objects/s into ~55-160 objects/s. Past roughly this point
#   the publisher is limited by the runner's ~1 Gbps NIC rather than by latency,
#   and raising it buys wall-clock only in exchange for disk and R2 concurrency.
# * RAM is 16 x the 1 MiB streaming chunk. Objects are never read whole into
#   memory, so this bound does not depend on object size at all -- strictly
#   stronger than the one-object-in-RAM bound #170 established.
# * Local disk is 16 x the largest object resident at once, because each worker
#   hydrates one staged object, publishes it and evicts it. MEASURED at the planet
#   address shape the largest published object is a 278 MB `.av1`, so 16 x 278 MB =
#   4.45 GB, 5.6x inside FINALIZE_FREE_DISK_FLOOR_BYTES -- and those objects are
#   hydrated from staging and evicted rather than newly materialized.
#
#   BUT the measurement is not the bound. `HOSTED_LIMITS[...]["max_serving_bytes"]`
#   admits an object of 2 GiB, and 16 x 2 GiB = 32 GiB is 1.28x OVER the floor. So the
#   worker count and the per-object cap are only jointly safe, and that relationship
#   is asserted (`test_the_concurrency_and_the_object_cap_fit_the_disk_floor`) rather
#   than left to hold by luck: raise `max_serving_bytes` and the assertion tells you
#   to lower PUBLISH_CONCURRENCY, or vice versa.
# * It is <= r2_verified_store.DEFAULT_MAX_POOL_CONNECTIONS, so no worker blocks on
#   the client's connection pool instead of on the network.
PUBLISH_CONCURRENCY = 16
assert PUBLISH_CONCURRENCY <= R2.DEFAULT_MAX_POOL_CONNECTIONS


def publication_concurrency(max_object_bytes: int) -> int:
    """Workers that fit the disk floor, given the largest object the run may admit.

    Local-disk peak is `concurrency` x the largest resident object, so the worker count
    and the per-object cap are only jointly safe and this is where that is resolved
    instead of being asserted after the fact. `PUBLISH_CONCURRENCY` is a CEILING here,
    not the answer: at the address family's admitted `max_serving_bytes` of 2 GiB,
    16 workers would be 32 GiB against a 25.6 GB floor -- 1.28x over -- so this returns
    11. The MEASURED largest `.av1` is 278 MB, at which 16 x 278 MB = 4.45 GB is 5.6x
    inside the floor; deriving rather than measuring is what keeps that true if a
    future partition produces a bigger object.

    Every caller must supply a positive cap from the run contract. Addresses has the
    narrower `max_serving_bytes`; Places uses its enforced `max_output_bytes`.
    Missing a cap is an admission defect, not permission to assume that 16 resident
    objects fit.
    """
    if not isinstance(max_object_bytes, int) or isinstance(max_object_bytes, bool) or max_object_bytes < 1:
        raise ValueError(
            f"largest admissible object size must be a positive integer, got "
            f"{max_object_bytes!r}"
        )
    return max(1, min(PUBLISH_CONCURRENCY, FINALIZE_FREE_DISK_FLOOR_BYTES // max_object_bytes))

CHUNK_BYTES = 1024 * 1024


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def stream_identity(source: BinaryIO) -> dict[str, object]:
    """Digest and length of everything left in ``source``, in bounded memory."""
    digest = hashlib.sha256()
    size = 0
    for chunk in iter(lambda: source.read(CHUNK_BYTES), b""):
        digest.update(chunk)
        size += len(chunk)
    return {"sha256": digest.hexdigest(), "bytes": size}


def file_identity(path: Path) -> dict[str, object]:
    with path.open("rb") as source:
        return stream_identity(source)


def listing_operations(keys: int) -> int:
    """Class-A operations one exact-prefix listing of ``keys`` objects costs."""
    return max(1, math.ceil(max(0, int(keys)) / LIST_PAGE_KEYS))


@dataclass
class Budget:
    max_operations: int
    max_write_bytes: int
    max_read_bytes: int
    operations: int = 0
    write_bytes: int = 0
    read_bytes: int = 0
    # The caps are fail-closed limits, and the upload pass charges them from
    # PUBLISH_CONCURRENCY threads. `self.operations += n` is a read-modify-write, so
    # without this a lost update would let a run publish past its admitted cap --
    # the exact overrun #173 added the plan-time gate to make impossible.
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def charge(self, *, operations: int = 1, write_bytes: int = 0, read_bytes: int = 0) -> None:
        with self.lock:
            self.operations += operations
            self.write_bytes += write_bytes
            self.read_bytes += read_bytes
            operations_total = self.operations
            write_total = self.write_bytes
            read_total = self.read_bytes
        if operations_total > self.max_operations:
            raise RuntimeError("remote operation cap exceeded")
        if write_total > self.max_write_bytes:
            raise RuntimeError("remote write-byte cap exceeded")
        if read_total > self.max_read_bytes:
            raise RuntimeError("remote read-byte cap exceeded")


class ConflictError(RuntimeError):
    pass


class _MD5Reader:
    """Wraps a reader and digests what is read through it.

    Used to learn the MD5 of the bytes the store was actually handed, so
    `VerifiedStoreRemote.read_back_identity` has something server-computed to compare
    against without re-downloading the object. Digesting what was READ, rather than
    re-reading the file afterwards, is what makes it the bytes that were SENT.

    It must survive ARBITRARY seeks, because the SDK does several kinds and none of
    them is a rewind-to-resend:

    * `determine_content_length` probes with `seek(0, 2)` then seeks back. That reads
      no bytes at all, so it must not disturb the digest -- and must not raise. An
      earlier version raised on any non-zero seek, which made every non-empty PUT over
      an https endpoint fail before a byte was sent.
    * a retry rewinds to 0 and re-reads. The digest has to describe the bytes that
      were ACTUALLY sent, so the second pass must REPLACE the first, not extend it.
    * a body that cannot seek at all makes a retryable 5xx fatal, so `seek` and `tell`
      are delegated rather than withheld.

    So the digest is tracked by POSITION rather than by interpreting seeks: it covers
    exactly the contiguous prefix `[0, digested)`, is restarted by any read beginning
    at 0, and is abandoned if a read ever starts somewhere else. `content_md5` then
    hands back a digest only when it covers the whole object as one pass, and None
    otherwise -- which is the fail-closed answer, because the caller's fallback is the
    full streaming read-back rather than a skipped check.
    """

    def __init__(self, source: BinaryIO):
        self._source = source
        self._digest = hashlib.md5(usedforsecurity=False)
        self._digested = 0
        self._contiguous = True

    def read(self, size: int = -1) -> bytes:
        start = self._source.tell()
        chunk = self._source.read(size)
        if start == 0:
            # The stream (re)starts here: whatever was digested before belongs to an
            # attempt whose bytes did not arrive.
            self._digest = hashlib.md5(usedforsecurity=False)
            self._digested = 0
            self._contiguous = True
        if self._contiguous and start == self._digested:
            self._digest.update(chunk)
            self._digested += len(chunk)
        elif chunk:
            self._contiguous = False
        return chunk

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        # Deliberately does NOT touch the digest. A seek moves no bytes; only a read
        # does, and `read` is where the accounting lives.
        return self._source.seek(offset, whence)

    def tell(self) -> int:
        return self._source.tell()

    def content_md5(self, size: int) -> str | None:
        """MD5 of the bytes sent, or None if they were not read as one whole pass."""
        if not self._contiguous or self._digested != size:
            return None
        return self._digest.hexdigest()


def _safe_key(key: str) -> str:
    if not key or key.startswith("/") or ".." in Path(key).parts:
        raise ValueError("unsafe remote key")
    return key


class FilesystemRemote:
    """Create-only reference backend; paths are always relative canonical keys."""

    # A plain file has nowhere to keep a digest, so this backend's HEAD is a LENGTH
    # comparison. Stated as an attribute rather than assumed, so `_expected_head`
    # cannot silently downgrade a backend that DOES record one.
    records_sha256_metadata = False

    def __init__(self, root: Path, budget: Budget):
        self.root = root
        self.budget = budget

    def path(self, key: str) -> Path:
        if key.startswith("/") or ".." in Path(key).parts:
            raise ValueError("unsafe remote key")
        return self.root / key

    def put_create_only(self, key: str, payload: bytes) -> None:
        self.budget.charge(write_bytes=len(payload))
        target = self.path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with target.open("xb") as output:
                output.write(payload)
        except FileExistsError as error:
            raise ConflictError(key) from error

    def put_create_only_stream(
        self, key: str, source: BinaryIO, *, size: int, sha256: str
    ) -> None:
        """Create ``key`` from an open reader, verifying the bytes it publishes.

        The copy is digested as it is written and linked into place only if it
        matches ``sha256``, so on this backend the bytes that become the object are
        provably the admitted bytes -- there is no window at all between the check
        and the publication. ``link(2)`` is the create-only step: unlike
        ``replace()`` it cannot overwrite an object that raced the write.
        """
        self.budget.charge(write_bytes=size)
        target = self.path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        written = 0
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as temporary:
            temporary_path = Path(temporary.name)
            for chunk in iter(lambda: source.read(CHUNK_BYTES), b""):
                digest.update(chunk)
                written += len(chunk)
                temporary.write(chunk)
        try:
            if (digest.hexdigest(), written) != (sha256, size):
                raise RuntimeError(
                    f"payload changed between admission and upload: {key}"
                )
            try:
                os.link(temporary_path, target)
            except FileExistsError as error:
                raise ConflictError(key) from error
        finally:
            temporary_path.unlink(missing_ok=True)

    def head(self, key: str) -> dict[str, object] | None:
        self.budget.charge()
        target = self.path(key)
        if not target.is_file():
            return None
        return {"bytes": target.stat().st_size}

    def stream(self, key: str) -> BinaryIO:
        target = self.path(key)
        self.budget.charge(read_bytes=target.stat().st_size)
        return target.open("rb")

    def list(self, prefix: str) -> list[str]:
        base = self.path(prefix)
        keys = (
            sorted(
                str(path.relative_to(self.root))
                for path in base.rglob("*")
                if path.is_file()
            )
            if base.exists()
            else []
        )
        # Charged AFTER the work, and per PAGE: a listing's request count is not
        # knowable before it runs, and a planet address slice is 66 pages, not one
        # request. Pricing it as one would make `finalize_remote_operations`
        # understate the phase it exists to bound.
        self.budget.charge(operations=listing_operations(len(keys)))
        return keys

    def read_back_identity(self, key: str, expected: dict[str, object]) -> dict[str, object]:
        """Whole-object re-read: stream it and digest it. One read per object.

        Cheap here by construction -- the "remote" is local disk -- so this backend
        keeps the strongest possible check and every offline test and slice run keeps
        exactly the semantics it had. `VerifiedStoreRemote` cannot afford it; see its
        override for what it does instead and why.
        """
        del expected
        return _stream_identity(self, key)

    def delete_exact(self, keys: Iterable[str], *, allowed_prefix: str, max_objects: int, max_bytes: int) -> dict[str, int]:
        exact = list(keys)
        if len(exact) > max_objects or any(not key.startswith(allowed_prefix) for key in exact):
            raise RuntimeError("cleanup scope exceeds its admitted exact set")
        total = sum(self.path(key).stat().st_size for key in exact if self.path(key).exists())
        if total > max_bytes:
            raise RuntimeError("cleanup byte cap exceeded")
        for key in exact:
            self.budget.charge()
            self.path(key).unlink(missing_ok=True)
        return {"objects": len(exact), "bytes": total}


class VerifiedStoreRemote:
    """Create-only publication straight into an ``r2_verified_store.ObjectStore``.

    Presents exactly the surface ``publish_exact_set`` and
    ``verify_whole_slice_once`` use, charges the same ``Budget``, and delegates
    every byte to the store -- so with ``FilesystemStore`` this whole path is
    exercisable offline and with the persistent R2 client it is the real
    publication. What it replaced was a local ``publish/`` tree plus a serial
    ``aws s3api`` loop in the workflow.

    ``head`` reports the store's recorded sha256 when there is one, which makes the
    per-upload verification a DIGEST comparison rather than the length comparison
    ``FilesystemRemote`` can offer.
    """

    # Both `ObjectStore` implementations record the sha256 as object metadata on
    # every create, so the per-upload HEAD here compares the digest and not only the
    # length -- and an object that comes back with NO recorded digest fails the
    # comparison rather than passing it.
    records_sha256_metadata = True

    def __init__(self, store: Any, budget: Budget):
        self.store = store
        self.budget = budget
        # MD5 of the bytes this process actually handed the store, per key, captured
        # as they streamed past. It is the only thing `read_back_identity` can compare
        # the store's own content digest against without downloading the object again.
        # Bounded: 32 hex characters per published key, ~4 MB for a 66,000-object
        # planet slice.
        self._sent_md5: dict[str, str] = {}
        self._sent_md5_lock = threading.Lock()

    def put_create_only(self, key: str, payload: bytes) -> None:
        """Create ``key`` from bytes already in memory -- the completion marker."""
        with tempfile.TemporaryFile() as handle:
            handle.write(payload)
            handle.seek(0)
            self.put_create_only_stream(
                key,
                handle,
                size=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
            )

    def put_create_only_stream(
        self, key: str, source: BinaryIO, *, size: int, sha256: str
    ) -> None:
        self.budget.charge(write_bytes=size)
        reader = _MD5Reader(source)
        try:
            self.store.upload_fileobj(reader, _safe_key(key), sha256, size=size)
        except FileExistsError as error:
            raise ConflictError(key) from error
        # Recorded only when the digest provably covers the whole object as one pass.
        # Absent means `read_back_identity` does the full streaming read-back instead
        # of trusting a partial digest -- the check gets slower, never weaker.
        sent = reader.content_md5(size)
        if sent is not None:
            with self._sent_md5_lock:
                self._sent_md5[key] = sent

    def read_back_identity(self, key: str, expected: dict[str, object]) -> dict[str, object]:
        """Verify a published object by METADATA, not by downloading it again.

        This is the single biggest cost in the phase and it is worth stating exactly
        what it does. Streaming every final object is a full re-download of the slice
        -- 13-18 GB for planet places and ~100-145 GB for addresses -- on top of the
        hydration and the upload. Against a persistent client the same guarantee is
        available from one metadata request, so this compares:

          * `bytes` against the store's CONTENT LENGTH -- server-authoritative;
          * the MD5 the store computed over the bytes it holds (the single-part ETag
            on R2) against the MD5 of the bytes this process actually sent, captured
            while they streamed past. Server-computed over the stored object, so this
            is evidence about what R2 holds and not an echo of a client claim;
          * `sha256` against the store's recorded sha256 metadata. That one IS a
            client echo -- it proves the metadata and the identity agree, nothing
            about the bytes -- so it is a cross-check, not the content proof.

        `single_part_etag_md5` fails closed on a multipart ETag rather than comparing
        something that is not a content digest. Publication goes through `put_object`,
        which is always single-part, so that cannot happen without a deliberate change.

        WHEN THERE IS NO SENT MD5 -- a RESUMED finalize, where the conflicting object
        was published by an earlier run -- this falls back to the full streaming
        read-back. Fail-closed: no local evidence means do the expensive honest thing,
        never skip the check. So a resume pays the download a first attempt does not.
        That check comes FIRST, before the metadata request, so the fallback costs
        exactly ONE operation like the fast path. Doing the HEAD and then falling back
        charged two, which on a resumed planet finalize is 5 operations per object
        against the 4 #173's projection prices -- a 25% overrun on the very cap the
        plan-time gate exists to guarantee.

        The residual, stated rather than glossed: on the fast path the content proof
        is MD5 rather than SHA-256. It is server-computed over immutable, create-only
        objects, and the SHA-256 of those same bytes was checked against the admitted
        identity immediately before the PUT (see `publish_exact_set`). What it does
        not cover is an adversary who can delete and recreate an object in this bucket
        AND craft an MD5 collision for it. `x-amz-checksum-sha256` would close that,
        and is deliberately NOT used here: R2's support for it is unverified, and
        depending on an unverified header for a fail-closed check is how a planet run
        dies at its last step.
        """
        with self._sent_md5_lock:
            sent = self._sent_md5.get(key)
        if sent is None:
            return _stream_identity(self, key)
        proof = self.store.head_proof(_safe_key(key))
        self.budget.charge()
        if proof is None:
            raise RuntimeError(f"final slice object is absent: {key}")
        if proof["content_md5"] != sent:
            raise RuntimeError(
                f"final slice identity differs: {key} (the store's content digest is "
                f"not the digest of the bytes that were published)"
            )
        if proof["sha256_metadata"] != expected["sha256"]:
            raise RuntimeError(
                f"final slice recorded sha256 differs from the admitted identity: {key}"
            )
        return {"sha256": expected["sha256"], "bytes": int(proof["bytes"])}

    def head(self, key: str) -> dict[str, object] | None:
        self.budget.charge()
        info = self.store.head(_safe_key(key))
        if info is None:
            return None
        head: dict[str, object] = {"bytes": int(info.bytes)}
        if info.sha256 is not None:
            head["sha256"] = info.sha256
        return head

    @contextlib.contextmanager
    def stream(self, key: str):
        size, handle = self.store.open_stream(_safe_key(key))
        self.budget.charge(read_bytes=size)
        try:
            yield handle
        finally:
            handle.close()

    def list(self, prefix: str) -> list[str]:
        keys = self.store.list_prefix(_safe_key(prefix))
        self.budget.charge(operations=listing_operations(len(keys)))
        return keys


def _stream_identity(remote: Any, key: str) -> dict[str, object]:
    with remote.stream(key) as source:
        return stream_identity(source)


def _run_bounded(
    work: Callable[[int, Any], None], items: Sequence[Any], *, concurrency: int
) -> None:
    """Run ``work(index, item)`` over ``items`` with at most ``concurrency`` in flight.

    Three properties this has to have, and all three are the reason it is written out
    rather than being ``ThreadPoolExecutor.map``:

    * **Bounded.** Work is submitted only as earlier work finishes, so the pool
      never holds more than ``concurrency`` hydrated objects, connections or futures.
      ``map`` submits the whole sequence at once.
    * **Submitted in order.** ``items`` is consumed front to back, so the admitted
      (sorted) set is offered to the remote in admitted order. Completion order is
      not ordered and cannot be -- that is what concurrency means -- but nothing
      downstream depends on it: the marker records the sorted set.
    * **Drained before it raises.** On the first failure nothing further is
      submitted, but every already-running task is awaited before the exception
      propagates. That is what keeps "the marker is written last" true under
      concurrency: when the caller decides whether to commit the marker, no upload
      is still in flight.
    """
    if concurrency < 1:
        raise ValueError("publication concurrency must be at least 1")
    pending: set[Future] = set()
    failure: BaseException | None = None
    remaining = iter(list(enumerate(items, 1)))
    with ThreadPoolExecutor(max_workers=concurrency) as pool:

        def fill() -> None:
            while len(pending) < concurrency:
                try:
                    index, item = next(remaining)
                except StopIteration:
                    return
                pending.add(pool.submit(work, index, item))

        fill()
        while pending:
            done, still_running = wait(pending, return_when=FIRST_COMPLETED)
            pending.clear()
            pending.update(still_running)
            for future in done:
                # BaseException too: a fail-closed gate raises SystemExit, and
                # concurrent.futures records it on the future rather than letting it
                # escape the worker thread.
                error = future.exception()
                if error is not None and failure is None:
                    failure = error
            if failure is None:
                fill()
    if failure is not None:
        raise failure


def _noop() -> None:
    return None


@dataclass(frozen=True)
class Member:
    """One member of an admitted set, plus how to make its bytes locally readable.

    `publish_exact_set` touches every member's file exactly twice -- once to hash
    it into the admitted set, once to read the payload it uploads -- and must not
    hold more than one member at a time in RAM or on disk. At planet scale the set
    is 13-18 GB (a ~10-11 GB head payload plus 3.3-6.7 GB of positions packs) on a
    16 GB runner with a bounded disk, so "hold them all" is an unconditional OOM.

    `hydrate` returns the local path, fetching the object if it is not resident;
    `release` drops the local copy. The publisher brackets each of its two reads
    with the pair, so a member whose bytes live in a remote staging tree is
    resident only while it is being read. `local_member` builds the degenerate
    case -- a file already on local disk, released never -- which is what a plain
    `(key, path)` tuple is normalized to.
    """

    key: str
    hydrate: Callable[[], Path]
    release: Callable[[], None] = field(default=_noop)


def local_member(key: str, path: Path) -> Member:
    """A member that is already on local disk and must never be evicted."""
    return Member(key=key, hydrate=lambda: path, release=_noop)


def _members(artifacts: Sequence[Member | tuple[str, Path]]) -> list[Member]:
    return [
        item if isinstance(item, Member) else local_member(item[0], item[1])
        for item in artifacts
    ]


def _expected_head(remote: Any, item: dict[str, object]) -> dict[str, object]:
    """What HEAD must return for a correctly published ``item``.

    Length always; the digest as well on a backend that records one. `FilesystemRemote`
    stores no metadata, so there it stays a LENGTH comparison -- which is precisely
    why the payload re-check in `_publish_one` is not redundant with it.
    """
    expected: dict[str, object] = {"bytes": item["bytes"]}
    # Attribute access, NOT getattr-with-a-default: a backend that forgets to
    # declare this must crash, not quietly get the weaker of the two checks.
    if remote.records_sha256_metadata:
        expected["sha256"] = item["sha256"]
    return expected


def publish_exact_set(
    remote: Any,
    *,
    artifacts: Sequence[Member | tuple[str, Path]],
    marker_key: str,
    request_sha256: str,
    fail_after_upload: int | None = None,
    verify: Callable[[str, dict[str, object]], None] | None = None,
    concurrency: int = PUBLISH_CONCURRENCY,
) -> dict[str, object]:
    """Upload an admitted set, HEAD each upload, and commit its marker last.

    Two phases, in this order and for this reason: EVERY member's identity is
    admitted before ANY upload starts, so the admitted set -- and therefore the
    marker that describes it and the byte-exact identity a conflicting retry is
    allowed to accept -- is fixed and sorted before a single byte is published.
    The upload pass runs `concurrency` members at a time; the admission pass does
    not, so the barrier between them is where "fixed before any upload" lives.

    ADMISSION is serial on purpose. It is the pass that runs the caller's
    fail-closed gates, so serial keeps the abort deterministic (the first offending
    member in set order, every time) and keeps admission's residency at exactly one
    object. It is also not the expensive pass: the 2N remote operations that turned
    the old shell mirror into 12.4 hours all live in the upload pass.

    Identities are computed by STREAMING each file, never by retaining its bytes:
    holding all payloads simultaneously to reuse them in the upload loop was a
    13-18 GB dict at planet scale. The upload pass streams too -- it never calls
    `read_bytes` -- so **peak RAM is `concurrency` x the 1 MiB chunk and does not
    depend on object size at all.** That is stronger than the one-whole-object bound
    #170 established, and it is what makes concurrency affordable: 16 workers over
    ~180 MB planet address objects would be ~3 GB of RAM if the payload were read
    whole, and is ~16 MiB streamed.

    `verify`, if given, is called as `verify(key, identity)` during admission with
    the identity just computed from the file. It runs strictly before any upload,
    which is what lets a caller reject an object on grounds the publisher knows
    nothing about (provenance, content-addressed key) without hydrating it a third
    time.
    """
    members = _members(artifacts)
    by_key = {member.key: member for member in members}
    admitted = []
    for member in members:
        # Resident only for the hash. `file_identity` streams in 1 MiB chunks, so
        # nothing here scales with the object's size either.
        path = member.hydrate()
        identity = {"key": member.key, **file_identity(path)}
        if verify is not None:
            verify(member.key, identity)
        # Released on the SUCCESS path only. An object that failed its admission
        # gate stays on disk for a human to look at -- the run is aborting anyway,
        # and evicting the offending bytes would destroy the only evidence of them.
        member.release()
        admitted.append(identity)
    admitted.sort(key=lambda item: str(item["key"]))
    if len({item["key"] for item in admitted}) != len(admitted):
        raise ValueError("duplicate artifact key")

    uploaded = 0
    counter = threading.Lock()
    interrupted = threading.Event()

    def _publish_one(_index: int, item: dict[str, object]) -> None:
        nonlocal uploaded
        if interrupted.is_set():
            return
        key = str(item["key"])
        member = by_key[key]
        # ONE handle for both the re-check and the upload. Two `open()` calls on the
        # same path can resolve to two different inodes -- which is exactly the
        # `release()`-then-refill window below -- so the bytes that are digested and
        # the bytes that are published come off the same descriptor.
        path = member.hydrate()
        with path.open("rb") as handle:
            # RESTORES "the identity and the payload are the same bytes". Before this
            # function was split into two passes that was true by construction -- one
            # read produced both the admitted digest and the uploaded bytes. Two
            # passes means two reads, and NOTHING else re-checks the second one:
            #   * a plain-tuple / `local_member` member (the two manifests) is never
            #     digest-verified on either read;
            #   * a staged member's re-hydration hits `StagedObjectStore.path()`'s
            #     `if path.is_file(): return path` short-circuit, so the second read
            #     is not digest-checked either -- and `release()` opens a window in
            #     which the cache slot is unverified-writable that did not exist
            #     before;
            #   * the per-upload HEAD below compares only `bytes` on a backend with
            #     no stored digest, so a SAME-LENGTH swap passes it.
            # Without this check the marker would be committed recording the GOOD
            # identity over BAD bytes, with the admission gate having passed on the
            # good bytes it read first. `verify_whole_slice_once` does catch it, but
            # that runs AFTER the marker.
            if stream_identity(handle) != {
                "sha256": item["sha256"],
                "bytes": item["bytes"],
            }:
                raise RuntimeError(
                    f"payload changed between admission and upload: {key}"
                )
            handle.seek(0)
            try:
                remote.put_create_only_stream(
                    key,
                    handle,
                    size=int(item["bytes"]),
                    sha256=str(item["sha256"]),
                )
            except ConflictError:
                # A retry may accept only the byte-exact object pre-admitted above.
                # EXISTENCE is not enough: the shell mirror this replaced accepted a
                # pre-existing object on a bare `head-object`, which for the marker
                # and the two manifests -- the only keys that are not
                # content-addressed -- accepted differing bytes as a successful
                # publish.
                if _stream_identity(remote, key) != {
                    "sha256": item["sha256"],
                    "bytes": item["bytes"],
                }:
                    raise RuntimeError(f"conflicting immutable object: {key}")
        # Evicted as soon as this worker is done reading it, so residency is bounded
        # by the worker count rather than by the set.
        member.release()
        head = remote.head(key)
        if head != _expected_head(remote, item):
            raise RuntimeError(f"per-upload HEAD verification failed: {key}")
        with counter:
            uploaded += 1
            reached = uploaded
        if fail_after_upload == reached:
            interrupted.set()
            raise RuntimeError("injected interruption before marker")

    # STRICTLY AFTER every member. `_run_bounded` does not return until the pool has
    # shut down, so no upload is in flight here -- and it raises rather than
    # returning if any member failed, so the marker is unreachable unless the whole
    # admitted set is published. The marker is the only thing that says the slice is
    # complete; committing it while an upload is still running would make that a lie.
    _run_bounded(_publish_one, admitted, concurrency=concurrency)

    marker = {
        "schema": "overture-construction-v1-create-only-marker-v1",
        "request_sha256": request_sha256,
        "artifacts": admitted,
        "exact_keys_sha256": hashlib.sha256(canonical(admitted)).hexdigest(),
    }
    marker_payload = canonical(marker)
    marker_identity = {
        "sha256": hashlib.sha256(marker_payload).hexdigest(),
        "bytes": len(marker_payload),
    }
    try:
        remote.put_create_only(marker_key, marker_payload)
    except ConflictError:
        if _stream_identity(remote, marker_key) != marker_identity:
            raise RuntimeError("conflicting completion marker")
    if remote.head(marker_key) != _expected_head(remote, marker_identity):
        raise RuntimeError("marker HEAD verification failed")
    return marker


def verify_whole_slice_once(
    remote: Any,
    *,
    prefix: str,
    expected: list[dict[str, object]],
    concurrency: int = PUBLISH_CONCURRENCY,
) -> dict[str, object]:
    """One listing and one read-back per final object; no fleet re-reads.

    The exact-set equality runs FIRST and serially, on the one listing, so a slice
    with a missing, extra or duplicate key is refused before any object is touched.

    HOW an object is read back is the BACKEND's decision, because the cost differs by
    orders of magnitude. `FilesystemRemote` streams and digests it -- local disk, so
    it keeps the strongest check. `VerifiedStoreRemote` verifies from one metadata
    request against the digest of the bytes it sent, because streaming would be a full
    re-download of the published slice (~145 GB for planet addresses) on top of the
    hydration and the upload; see its `read_back_identity` for exactly what is
    compared and what the residual is.

    The per-object pass is `concurrency`-way either way. Results are stored by listing
    position, so `verified` -- and therefore `binding_sha256` -- is in key order
    regardless of completion order.
    """
    keys = remote.list(prefix)
    expected_by_key = {str(item["key"]): item for item in expected}
    if keys != sorted(expected_by_key):
        raise RuntimeError("final slice has missing, extra, or duplicate keys")
    verified: list[dict[str, object] | None] = [None] * len(keys)

    def _verify_one(index: int, key: str) -> None:
        wanted = expected_by_key[key]
        actual = remote.read_back_identity(key, wanted)
        if actual != {"sha256": wanted["sha256"], "bytes": wanted["bytes"]}:
            raise RuntimeError(f"final slice identity differs: {key}")
        verified[index - 1] = {"key": key, **actual}

    _run_bounded(_verify_one, keys, concurrency=concurrency)
    if any(item is None for item in verified):
        raise RuntimeError("final slice verification did not cover every object")
    return {"schema": "overture-construction-v1-whole-slice-verification-v1", "objects": len(verified), "bytes": sum(int(item["bytes"]) for item in verified), "binding_sha256": hashlib.sha256(canonical(verified)).hexdigest()}
