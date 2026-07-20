#!/usr/bin/env python3
"""Bounded concurrent prefetch transport for sequential fragment consumers.

The global address planner deliberately scans and verifies one fragment at a
time.  This adapter preserves that consumer contract while overlapping the
network reads for a manifest-bound sequence.  A long-running server owns the
bounded cache; the planner invokes the short-lived ``fetch`` client through its
existing no-shell argv interface.

The planner remains responsible for validating every fragment's bytes and
SHA-256 against the request-bound map manifest.  This transport additionally
checks the declared byte length before releasing a prefetched object.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import errno
import hashlib
import json
import os
import shutil
import socket
import socketserver
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any


MAX_WORKERS = 32
MAX_CACHE_BYTES = 8_000_000_000
MAX_MANIFEST_ENTRIES = 1_000_000
MAX_MESSAGE_BYTES = 64 * 1024


def _load_manifest(path: Path) -> list[dict[str, Any]]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("prefetch manifest is not readable JSON") from exc
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_MANIFEST_ENTRIES:
        raise ValueError("prefetch manifest entry count is outside hard bounds")
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict) or set(raw) != {"object_key", "bytes"}:
            raise ValueError("prefetch manifest entry is not exact")
        key, size = raw["object_key"], raw["bytes"]
        if (
            not isinstance(key, str)
            or not key
            or "\x00" in key
            or key in seen
            or type(size) is not int
            or size <= 0
        ):
            raise ValueError("prefetch manifest entry is invalid or duplicated")
        seen.add(key)
        entries.append({"object_key": key, "bytes": size})
    return entries


def _load_fetch_command(value: str) -> list[str]:
    try:
        command = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("prefetch command must be a JSON argv array") from exc
    if (
        not isinstance(command, list)
        or not command
        or any(not isinstance(item, str) or not item for item in command)
        or sum(item.count("{object_key}") for item in command) < 1
        or sum(item.count("{output}") for item in command) != 1
    ):
        raise ValueError(
            "prefetch command must contain {object_key} and exactly one {output}"
        )
    return command


class PrefetchManager:
    def __init__(
        self,
        entries: list[dict[str, Any]],
        *,
        cache_dir: Path,
        fetch_command: list[str],
        workers: int,
        max_cache_bytes: int,
    ) -> None:
        if (
            not 1 <= workers <= MAX_WORKERS
            or not 1 <= max_cache_bytes <= MAX_CACHE_BYTES
            or any(entry["bytes"] > max_cache_bytes for entry in entries)
        ):
            raise ValueError("prefetch concurrency or cache is outside hard bounds")
        cache_dir.mkdir(parents=True, exist_ok=True)
        if any(cache_dir.iterdir()):
            raise ValueError("prefetch cache directory must start empty")
        self.entries = entries
        self.cache_dir = cache_dir
        self.fetch_command = fetch_command
        self.max_cache_bytes = max_cache_bytes
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
        self.futures: dict[int, concurrent.futures.Future[Path]] = {}
        self.next_schedule = 0
        self.next_consume = 0
        self.reserved_bytes = 0
        self.lock = threading.Lock()
        with self.lock:
            self._schedule_locked()

    @property
    def done(self) -> bool:
        with self.lock:
            return self.next_consume == len(self.entries)

    def _cache_path(self, index: int, key: str, suffix: str) -> Path:
        digest = hashlib.sha256(key.encode()).hexdigest()[:20]
        return self.cache_dir / f"{index:07d}-{digest}.{suffix}"

    def _download(self, index: int, entry: dict[str, Any]) -> Path:
        partial = self._cache_path(index, entry["object_key"], "partial")
        ready = self._cache_path(index, entry["object_key"], "ready")
        if partial.exists() or ready.exists():
            raise ValueError("prefetch cache target unexpectedly exists")
        argv = [
            item.replace("{object_key}", entry["object_key"]).replace(
                "{output}", str(partial)
            )
            for item in self.fetch_command
        ]
        try:
            result = subprocess.run(argv, text=True, capture_output=True)
            if result.returncode != 0:
                detail = (result.stderr or result.stdout)[-2_000:].strip()
                raise ValueError(f"prefetch command failed: {detail}")
            if not partial.is_file() or partial.stat().st_size != entry["bytes"]:
                raise ValueError("prefetched fragment byte length differs")
            os.replace(partial, ready)
            return ready
        finally:
            partial.unlink(missing_ok=True)

    def _schedule_locked(self) -> None:
        while self.next_schedule < len(self.entries):
            entry = self.entries[self.next_schedule]
            if self.reserved_bytes + entry["bytes"] > self.max_cache_bytes:
                break
            index = self.next_schedule
            self.futures[index] = self.executor.submit(self._download, index, entry)
            self.reserved_bytes += entry["bytes"]
            self.next_schedule += 1

    def provide(self, object_key: str, output: Path) -> None:
        with self.lock:
            index = self.next_consume
            if index >= len(self.entries):
                raise ValueError("prefetch sequence is already complete")
            entry = self.entries[index]
            if object_key != entry["object_key"]:
                raise ValueError("fragment consumer departed from manifest order")
            future = self.futures.get(index)
            if future is None:
                raise ValueError("manifest head was not admitted to the bounded cache")
        ready = future.result()
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists():
            raise ValueError("fragment consumer output already exists")
        try:
            os.replace(ready, output)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            shutil.copyfile(ready, output)
            ready.unlink()
        with self.lock:
            if self.next_consume != index:
                raise ValueError("fragment prefetch consumption raced")
            self.reserved_bytes -= entry["bytes"]
            del self.futures[index]
            self.next_consume += 1
            self._schedule_locked()
            consumed = self.next_consume
            total = len(self.entries)
        if consumed == 1 or consumed % 100 == 0 or consumed == total:
            print(
                f"prefetch consumed {consumed}/{total} manifest fragments",
                file=sys.stderr,
                flush=True,
            )

    def close(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=True)


class PrefetchServer(socketserver.UnixStreamServer):
    allow_reuse_address = False

    def __init__(self, socket_path: Path, manager: PrefetchManager) -> None:
        self.manager = manager
        super().__init__(str(socket_path), PrefetchHandler)


class PrefetchHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        raw = self.rfile.readline(MAX_MESSAGE_BYTES + 1)
        try:
            if not raw or len(raw) > MAX_MESSAGE_BYTES or not raw.endswith(b"\n"):
                raise ValueError("prefetch request is missing or oversized")
            value = json.loads(raw)
            if not isinstance(value, dict) or set(value) != {"object_key", "output"}:
                raise ValueError("prefetch request is not exact")
            key, output = value["object_key"], value["output"]
            if not isinstance(key, str) or not key or not isinstance(output, str) or not output:
                raise ValueError("prefetch request fields are invalid")
            self.server.manager.provide(key, Path(output))  # type: ignore[attr-defined]
            response = {"ok": True}
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            response = {"ok": False, "error": str(exc)}
        self.wfile.write(json.dumps(response, sort_keys=True).encode() + b"\n")


def serve(
    *,
    manifest: Path,
    socket_path: Path,
    cache_dir: Path,
    fetch_command_json: str,
    workers: int,
    max_cache_bytes: int,
) -> None:
    if socket_path.exists():
        raise ValueError("prefetch socket path already exists")
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    manager = PrefetchManager(
        _load_manifest(manifest),
        cache_dir=cache_dir,
        fetch_command=_load_fetch_command(fetch_command_json),
        workers=workers,
        max_cache_bytes=max_cache_bytes,
    )
    try:
        with PrefetchServer(socket_path, manager) as server:
            server.timeout = 1.0
            while not manager.done:
                server.handle_request()
    finally:
        manager.close()
        socket_path.unlink(missing_ok=True)


def fetch(*, socket_path: Path, object_key: str, output: Path) -> None:
    request = json.dumps(
        {"object_key": object_key, "output": str(output)}, sort_keys=True
    ).encode() + b"\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(30 * 60)
        client.connect(str(socket_path))
        client.sendall(request)
        received = bytearray()
        while not received.endswith(b"\n"):
            chunk = client.recv(MAX_MESSAGE_BYTES + 1 - len(received))
            if not chunk:
                break
            received.extend(chunk)
            if len(received) > MAX_MESSAGE_BYTES:
                raise ValueError("prefetch response is oversized")
    try:
        response = json.loads(received)
    except json.JSONDecodeError as exc:
        raise ValueError("prefetch server returned invalid JSON") from exc
    if not isinstance(response, dict) or response.get("ok") is not True:
        detail = response.get("error") if isinstance(response, dict) else None
        raise ValueError(f"prefetch server rejected fragment: {detail}")
    if not output.is_file():
        raise ValueError("prefetch server produced no fragment")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    server = commands.add_parser("serve")
    server.add_argument("--manifest", type=Path, required=True)
    server.add_argument("--socket", type=Path, required=True)
    server.add_argument("--cache-dir", type=Path, required=True)
    server.add_argument("--fetch-command-json", required=True)
    server.add_argument("--workers", type=int, default=16)
    server.add_argument("--max-cache-bytes", type=int, default=2_000_000_000)
    client = commands.add_parser("fetch")
    client.add_argument("--socket", type=Path, required=True)
    client.add_argument("--object-key", required=True)
    client.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "serve":
        serve(
            manifest=args.manifest,
            socket_path=args.socket,
            cache_dir=args.cache_dir,
            fetch_command_json=args.fetch_command_json,
            workers=args.workers,
            max_cache_bytes=args.max_cache_bytes,
        )
    else:
        fetch(socket_path=args.socket, object_key=args.object_key, output=args.output)


if __name__ == "__main__":
    main()
