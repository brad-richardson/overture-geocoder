#!/usr/bin/env python3
"""Bounded, create-only remote publication primitives for construction-v1.

The implementation is intentionally backend-neutral and is tested with a local
filesystem. Hosted credentials belong in the workflow adapter, never here.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterable


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


def publish_exact_set(
    remote: FilesystemRemote,
    *,
    artifacts: list[tuple[str, Path]],
    marker_key: str,
    request_sha256: str,
    fail_after_upload: int | None = None,
) -> dict[str, object]:
    """Upload an admitted set, HEAD each upload, and commit its marker last."""
    admitted = []
    payloads: dict[str, bytes] = {}
    for key, path in artifacts:
        payload = path.read_bytes()
        identity = {"key": key, "sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)}
        admitted.append(identity)
        payloads[key] = payload
    admitted.sort(key=lambda item: str(item["key"]))
    if len({item["key"] for item in admitted}) != len(admitted):
        raise ValueError("duplicate artifact key")

    for index, item in enumerate(admitted, 1):
        key = str(item["key"])
        try:
            remote.put_create_only(key, payloads[key])
        except ConflictError:
            # A retry may accept only the byte-exact object pre-admitted above.
            if _stream_identity(remote, key) != {"sha256": item["sha256"], "bytes": item["bytes"]}:
                raise RuntimeError(f"conflicting immutable object: {key}")
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
