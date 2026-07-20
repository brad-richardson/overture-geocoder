#!/usr/bin/env python3
"""Upload and restore immutable artifacts with size and SHA-256 verification.

The object identity includes the content digest. An existing object is never
overwritten: it is downloaded and verified before being accepted as completed.
Downloads likewise land in a temporary file and replace a stale destination
only after verification. The filesystem backend makes the restart contract
testable without credentials; the S3 backend targets Cloudflare R2 via ``aws``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol


SCHEMA = "overture-verified-shuffle-manifest-v1"
SHA_METADATA_KEY = "sha256"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_identity(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"artifact is not a regular file: {path}")
    return {
        "path": str(path),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def immutable_key(prefix: str, artifact: dict[str, Any]) -> str:
    digest = artifact["sha256"]
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise ValueError("artifact SHA-256 is not canonical lowercase hex")
    name = PurePosixPath(str(artifact["path"])).name
    if name in ("", ".", ".."):
        raise ValueError("artifact path has no safe basename")
    clean_prefix = prefix.strip("/")
    return str(PurePosixPath(clean_prefix, "sha256", digest, name))


@dataclass(frozen=True)
class ObjectInfo:
    bytes: int
    sha256: str | None


class ObjectStore(Protocol):
    def head(self, key: str) -> ObjectInfo | None: ...

    def list_prefix(self, prefix: str) -> list[str]: ...

    def upload(self, source: Path, key: str, sha256: str) -> None:
        """Create ``key`` atomically, raising FileExistsError if it exists."""
        ...

    def download(self, key: str, destination: Path) -> None: ...


class FilesystemStore:
    """Atomic local object store used by tests and offline rehearsals."""

    def __init__(self, root: Path):
        self.root = root

    def _path(self, key: str) -> Path:
        relative = PurePosixPath(key)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("object key escapes store root")
        return self.root.joinpath(*relative.parts)

    def head(self, key: str) -> ObjectInfo | None:
        path = self._path(key)
        if not path.exists():
            return None
        metadata = path.with_name(f"{path.name}.metadata.json")
        payload = json.loads(metadata.read_text()) if metadata.exists() else {}
        return ObjectInfo(path.stat().st_size, payload.get(SHA_METADATA_KEY))

    def list_prefix(self, prefix: str) -> list[str]:
        return sorted(
            path.relative_to(self.root).as_posix()
            for path in self.root.rglob("*")
            if path.is_file()
            and not path.name.endswith(".metadata.json")
            and path.relative_to(self.root).as_posix().startswith(prefix)
        )

    def upload(self, source: Path, key: str, sha256: str) -> None:
        destination = self._path(key)
        if destination.exists():
            raise FileExistsError(f"refusing to overwrite object: {key}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=destination.parent, delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
            with source.open("rb") as input_file:
                shutil.copyfileobj(input_file, temporary)
        metadata_path = destination.with_name(f"{destination.name}.metadata.json")
        try:
            # link(2) is an atomic create-only publication: unlike replace(), it
            # cannot overwrite an object that races the preflight exists check.
            os.link(temporary_path, destination)
            metadata_path.write_text(
                json.dumps({SHA_METADATA_KEY: sha256}, sort_keys=True) + "\n"
            )
        finally:
            temporary_path.unlink(missing_ok=True)

    def download(self, key: str, destination: Path) -> None:
        source = self._path(key)
        if not source.is_file():
            raise FileNotFoundError(key)
        shutil.copyfile(source, destination)


class S3Store:
    """Minimal AWS CLI adapter for an S3-compatible R2 bucket."""

    def __init__(self, bucket: str, endpoint_url: str):
        self.bucket = bucket
        self.endpoint_url = endpoint_url

    def _run(
        self, arguments: list[str], *, capture: bool = False
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "aws",
                "s3api",
                *arguments,
                "--endpoint-url",
                self.endpoint_url,
                "--region",
                "auto",
            ],
            check=True,
            text=True,
            capture_output=capture,
        )

    def head(self, key: str) -> ObjectInfo | None:
        command = [
            "aws",
            "s3api",
            "head-object",
            "--bucket",
            self.bucket,
            "--key",
            key,
            "--endpoint-url",
            self.endpoint_url,
            "--region",
            "auto",
            "--output",
            "json",
        ]
        result = subprocess.run(command, text=True, capture_output=True)
        if result.returncode != 0:
            combined = f"{result.stdout}\n{result.stderr}"
            if "404" in combined or "Not Found" in combined or "NoSuchKey" in combined:
                return None
            raise RuntimeError(f"head-object failed for {key}: {result.stderr.strip()}")
        payload = json.loads(result.stdout)
        metadata = payload.get("Metadata") or {}
        return ObjectInfo(int(payload["ContentLength"]), metadata.get(SHA_METADATA_KEY))

    def list_prefix(self, prefix: str) -> list[str]:
        if not isinstance(prefix, str) or not prefix or prefix.startswith("/"):
            raise ValueError("R2 list prefix must be a non-empty relative key prefix")
        keys: list[str] = []
        seen_keys: set[str] = set()
        seen_tokens: set[str] = set()
        continuation: str | None = None
        while True:
            arguments = [
                "list-objects-v2",
                "--bucket",
                self.bucket,
                "--prefix",
                prefix,
                "--max-keys",
                "1000",
                "--output",
                "json",
                "--no-paginate",
            ]
            if continuation is not None:
                arguments.extend(["--continuation-token", continuation])
            result = self._run(arguments, capture=True)
            try:
                payload = json.loads(result.stdout)
            except json.JSONDecodeError as exc:
                raise ValueError("R2 list page is not JSON") from exc
            contents = payload.get("Contents", [])
            truncated = payload.get("IsTruncated")
            if not isinstance(contents, list) or type(truncated) is not bool:
                raise ValueError("R2 list page has invalid contents/truncation fields")
            for item in contents:
                key = item.get("Key") if isinstance(item, dict) else None
                if not isinstance(key, str) or not key.startswith(prefix):
                    raise ValueError("R2 list result escaped its requested prefix")
                if key in seen_keys:
                    raise ValueError("R2 paginated listing contains a duplicate key")
                seen_keys.add(key)
                keys.append(key)
            token = payload.get("NextContinuationToken")
            if not truncated:
                if token not in (None, ""):
                    raise ValueError("R2 terminal list page unexpectedly has a token")
                break
            if not isinstance(token, str) or not token or token in seen_tokens:
                raise ValueError("R2 truncated list page has no fresh continuation token")
            seen_tokens.add(token)
            continuation = token
        return sorted(keys)

    def upload(self, source: Path, key: str, sha256: str) -> None:
        command = [
            "aws",
            "s3api",
            "put-object",
            "--bucket",
            self.bucket,
            "--key",
            key,
            "--body",
            str(source),
            "--metadata",
            f"{SHA_METADATA_KEY}={sha256}",
            "--if-none-match",
            "*",
            "--endpoint-url",
            self.endpoint_url,
            "--region",
            "auto",
        ]
        result = subprocess.run(command, text=True, capture_output=True)
        if result.returncode == 0:
            return
        combined = f"{result.stdout}\n{result.stderr}"
        if "PreconditionFailed" in combined or "412" in combined:
            raise FileExistsError(f"object appeared during create-only upload: {key}")
        raise RuntimeError(f"put-object failed for {key}: {result.stderr.strip()}")

    def download(self, key: str, destination: Path) -> None:
        self._run(
            [
                "get-object",
                "--bucket",
                self.bucket,
                "--key",
                key,
                str(destination),
            ]
        )


def verify_file(path: Path, *, expected_bytes: int, expected_sha256: str) -> None:
    if path.stat().st_size != expected_bytes:
        raise ValueError(f"artifact byte count differs: {path}")
    if sha256_file(path) != expected_sha256:
        raise ValueError(f"artifact SHA-256 differs: {path}")


def verified_download(
    store: ObjectStore,
    key: str,
    destination: Path,
    *,
    expected_bytes: int,
    expected_sha256: str,
) -> str:
    if destination.exists():
        try:
            verify_file(
                destination,
                expected_bytes=expected_bytes,
                expected_sha256=expected_sha256,
            )
            return "local_verified"
        except ValueError:
            pass
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{destination.name}.",
        suffix=".download",
        dir=destination.parent,
        delete=False,
    ) as temporary:
        temporary_path = Path(temporary.name)
    try:
        store.download(key, temporary_path)
        verify_file(
            temporary_path,
            expected_bytes=expected_bytes,
            expected_sha256=expected_sha256,
        )
        os.replace(temporary_path, destination)
    finally:
        temporary_path.unlink(missing_ok=True)
    return "remote_verified"


def ensure_uploaded(store: ObjectStore, source: Path, key: str) -> dict[str, Any]:
    artifact = artifact_identity(source)
    expected = ObjectInfo(artifact["bytes"], artifact["sha256"])
    remote = store.head(key)
    status = "uploaded"
    if remote is None:
        try:
            store.upload(source, key, artifact["sha256"])
        except FileExistsError:
            # A concurrent create won after our HEAD. Never retry with an
            # overwrite: inspect and read back the winner under the same rules
            # as an object that existed before the operation.
            remote = store.head(key)
            status = "existing_verified"
    if remote is not None:
        if remote != expected:
            raise ValueError(
                f"existing object identity differs; refusing overwrite: {key}"
            )
        status = "existing_verified"
    with tempfile.TemporaryDirectory(prefix="verified-shuffle-readback-") as directory:
        readback = Path(directory) / source.name
        verified_download(
            store,
            key,
            readback,
            expected_bytes=artifact["bytes"],
            expected_sha256=artifact["sha256"],
        )
    after = store.head(key)
    if after != expected:
        raise ValueError(f"remote object identity changed during verification: {key}")
    return {**artifact, "key": key, "status": status, "readback_verified": True}


def load_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text())
    if payload.get("schema") != SCHEMA or not isinstance(
        payload.get("artifacts"), list
    ):
        raise ValueError("invalid verified-shuffle manifest")
    return payload


def upload_manifest(
    store: ObjectStore, manifest_path: Path, prefix: str
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    uploaded = []
    for expected in manifest["artifacts"]:
        source = Path(expected["path"])
        actual = artifact_identity(source)
        if (actual["bytes"], actual["sha256"]) != (
            expected["bytes"],
            expected["sha256"],
        ):
            raise ValueError(f"local artifact differs from manifest: {source}")
        key = immutable_key(prefix, actual)
        if expected.get("key") not in (None, key):
            raise ValueError(f"manifest key differs from immutable identity: {source}")
        uploaded.append(ensure_uploaded(store, source, key))
    return {"schema": SCHEMA, "artifacts": uploaded}


def restore_manifest(
    store: ObjectStore, manifest_path: Path, output_dir: Path
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    restored = []
    destinations: set[Path] = set()
    for artifact in manifest["artifacts"]:
        key = artifact.get("key")
        if not isinstance(key, str) or not key:
            raise ValueError("manifest artifact is missing its immutable key")
        expected = ObjectInfo(artifact["bytes"], artifact["sha256"])
        if store.head(key) != expected:
            raise ValueError(f"remote object identity differs from manifest: {key}")
        destination = output_dir / PurePosixPath(str(artifact["path"])).name
        if destination in destinations:
            raise ValueError("manifest artifact basenames are not unique")
        destinations.add(destination)
        status = verified_download(
            store,
            key,
            destination,
            expected_bytes=artifact["bytes"],
            expected_sha256=artifact["sha256"],
        )
        restored.append(
            {
                **artifact,
                "path": str(destination),
                "status": status,
                "verified": True,
            }
        )
    return {"schema": SCHEMA, "artifacts": restored}


def build_manifest(paths: list[Path], prefix: str) -> dict[str, Any]:
    artifacts = []
    for path in paths:
        artifact = artifact_identity(path)
        artifacts.append({**artifact, "key": immutable_key(prefix, artifact)})
    return {"schema": SCHEMA, "artifacts": artifacts}


def store_from_args(args: argparse.Namespace) -> ObjectStore:
    if args.store_root is not None:
        return FilesystemStore(args.store_root)
    if not args.bucket or not args.endpoint_url:
        raise ValueError("R2 mode requires --bucket and --endpoint-url")
    return S3Store(args.bucket, args.endpoint_url)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--store-root", type=Path)
    parser.add_argument("--bucket")
    parser.add_argument("--endpoint-url")
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build-manifest")
    build.add_argument("--prefix", required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("paths", nargs="+", type=Path)

    upload = subparsers.add_parser("upload-manifest")
    upload.add_argument("--prefix", required=True)
    upload.add_argument("--manifest", type=Path, required=True)
    upload.add_argument("--report", type=Path, required=True)

    restore = subparsers.add_parser("restore-manifest")
    restore.add_argument("--manifest", type=Path, required=True)
    restore.add_argument("--output-dir", type=Path, required=True)
    restore.add_argument("--report", type=Path, required=True)

    args = parser.parse_args()
    if args.command == "build-manifest":
        payload = build_manifest(args.paths, args.prefix)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        return
    store = store_from_args(args)
    if args.command == "upload-manifest":
        payload = upload_manifest(store, args.manifest, args.prefix)
    else:
        payload = restore_manifest(store, args.manifest, args.output_dir)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
