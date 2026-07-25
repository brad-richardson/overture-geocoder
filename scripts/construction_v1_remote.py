#!/usr/bin/env python3
"""Bounded, create-only remote publication primitives for construction-v1.

The implementation is intentionally backend-neutral and is tested with a local
filesystem. Hosted credentials belong in the workflow adapter, never here.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Callable, Iterable, Sequence


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def file_identity(path: Path) -> dict[str, object]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return {"sha256": digest.hexdigest(), "bytes": size}


@dataclass
class Budget:
    max_operations: int
    max_write_bytes: int
    max_read_bytes: int
    operations: int = 0
    write_bytes: int = 0
    read_bytes: int = 0

    def charge(self, *, operations: int = 1, write_bytes: int = 0, read_bytes: int = 0) -> None:
        self.operations += operations
        self.write_bytes += write_bytes
        self.read_bytes += read_bytes
        if self.operations > self.max_operations:
            raise RuntimeError("remote operation cap exceeded")
        if self.write_bytes > self.max_write_bytes:
            raise RuntimeError("remote write-byte cap exceeded")
        if self.read_bytes > self.max_read_bytes:
            raise RuntimeError("remote read-byte cap exceeded")


class ConflictError(RuntimeError):
    pass


class FilesystemRemote:
    """Create-only reference backend; paths are always relative canonical keys."""

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
        self.budget.charge()
        base = self.path(prefix)
        if not base.exists():
            return []
        return sorted(str(path.relative_to(self.root)) for path in base.rglob("*") if path.is_file())

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


def _stream_identity(remote: FilesystemRemote, key: str) -> dict[str, object]:
    digest = hashlib.sha256()
    size = 0
    with remote.stream(key) as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return {"sha256": digest.hexdigest(), "bytes": size}


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


def publish_exact_set(
    remote: FilesystemRemote,
    *,
    artifacts: Sequence[Member | tuple[str, Path]],
    marker_key: str,
    request_sha256: str,
    fail_after_upload: int | None = None,
    verify: Callable[[str, dict[str, object]], None] | None = None,
) -> dict[str, object]:
    """Upload an admitted set, HEAD each upload, and commit its marker last.

    Two phases, in this order and for this reason: EVERY member's identity is
    admitted before ANY upload starts, so the admitted set -- and therefore the
    marker that describes it and the byte-exact identity a conflicting retry is
    allowed to accept -- is fixed and sorted before a single byte is published.

    Identities are computed by STREAMING each file (`file_identity`), never by
    retaining its bytes: holding all payloads simultaneously to reuse them in the
    upload loop was a 13-18 GB dict at planet scale. Each payload is re-read
    inside the upload loop instead, so peak RAM is one object -- bounded by the
    partition cap (512 MiB estimated uncompressed) rather than by the set size.

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

    for index, item in enumerate(admitted, 1):
        key = str(item["key"])
        member = by_key[key]
        # Re-read the payload HERE, where it is needed, instead of carrying every
        # payload down from admission in a dict. This read is the whole RAM bound.
        path = member.hydrate()
        payload = path.read_bytes()
        try:
            remote.put_create_only(key, payload)
        except ConflictError:
            # A retry may accept only the byte-exact object pre-admitted above.
            if _stream_identity(remote, key) != {"sha256": item["sha256"], "bytes": item["bytes"]}:
                raise RuntimeError(f"conflicting immutable object: {key}")
        # One object's bytes at a time: drop this payload before the next iteration
        # hydrates and reads its own, so peak RAM is one object rather than the set.
        del payload
        member.release()
        head = remote.head(key)
        if head != {"bytes": item["bytes"]}:
            raise RuntimeError(f"per-upload HEAD verification failed: {key}")
        if fail_after_upload == index:
            raise RuntimeError("injected interruption before marker")

    marker = {
        "schema": "overture-construction-v1-create-only-marker-v1",
        "request_sha256": request_sha256,
        "artifacts": admitted,
        "exact_keys_sha256": hashlib.sha256(canonical(admitted)).hexdigest(),
    }
    marker_payload = canonical(marker)
    try:
        remote.put_create_only(marker_key, marker_payload)
    except ConflictError:
        if _stream_identity(remote, marker_key) != {"sha256": hashlib.sha256(marker_payload).hexdigest(), "bytes": len(marker_payload)}:
            raise RuntimeError("conflicting completion marker")
    if remote.head(marker_key) != {"bytes": len(marker_payload)}:
        raise RuntimeError("marker HEAD verification failed")
    return marker


def verify_whole_slice_once(remote: FilesystemRemote, *, prefix: str, expected: list[dict[str, object]]) -> dict[str, object]:
    """One listing and one streaming read per final object; no fleet re-reads."""
    keys = remote.list(prefix)
    expected_by_key = {str(item["key"]): item for item in expected}
    if keys != sorted(expected_by_key):
        raise RuntimeError("final slice has missing, extra, or duplicate keys")
    verified = []
    for key in keys:
        actual = _stream_identity(remote, key)
        wanted = expected_by_key[key]
        if actual != {"sha256": wanted["sha256"], "bytes": wanted["bytes"]}:
            raise RuntimeError(f"final slice identity differs: {key}")
        verified.append({"key": key, **actual})
    return {"schema": "overture-construction-v1-whole-slice-verification-v1", "objects": len(verified), "bytes": sum(int(item["bytes"]) for item in verified), "binding_sha256": hashlib.sha256(canonical(verified)).hexdigest()}
