#!/usr/bin/env python3
"""Upload and restore immutable artifacts with stored-byte verification.

The object identity includes the content digest. An existing object is never
overwritten: its store-computed single-part ETag, length, and recorded SHA-256
metadata are verified before it is accepted as completed. Downloads land in a
temporary file and replace a stale destination only after their bytes and
metadata are verified. The filesystem backend makes the restart contract
testable without credentials; the S3 backends target Cloudflare R2.

There are two S3 backends and the difference is throughput, not discipline.
``S3Store`` shells out to ``aws s3api`` once per operation; ``Boto3Store`` holds
one persistent client. Both apply the same create-only (``If-None-Match: '*'``),
sha256-metadata and paginated-listing rules, and the listing validation is
literally the same function, so a fail-closed rule cannot hold on one and not the
other. ``s3_object_store`` is the only producer construction-v1 uses and it
returns the persistent one; see its docstring for the measurement that forces it.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Iterator, Protocol


SCHEMA = "overture-verified-shuffle-manifest-v1"
SHA_METADATA_KEY = "sha256"
# Keys per ListObjectsV2 page. Both S3 backends request exactly this, so a
# listing's class-A operation count is a function of the key count and nothing
# else -- which is what lets construction_v1_remote price a listing honestly
# instead of calling it one operation.
LIST_PAGE_KEYS = 1000


def listing_pages(keys: int) -> int:
    """ListObjectsV2 pages one exact-prefix listing of ``keys`` objects costs.

    A planet address slice is 65,751 objects, so its single "one listing" is 66
    billed requests. Charging one would understate the finalize operation budget,
    and #173 exists precisely because that budget was not what the phase spends.
    An empty prefix still costs the one request that discovers it is empty.
    """
    return max(1, math.ceil(max(0, int(keys)) / LIST_PAGE_KEYS))


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

    def upload_fileobj(
        self, source: BinaryIO, key: str, sha256: str, *, size: int
    ) -> None:
        """Create ``key`` from an already-open reader positioned at its start.

        Same create-only contract as ``upload``. It exists so a publisher can hash
        an object and upload it through ONE file handle: two ``open()`` calls on the
        same path are two different inodes if something unlinks and recreates the
        file in between, and that is exactly the cache-refill window
        ``construction_v1_remote.publish_exact_set`` has to close.
        """
        ...

    def download(self, key: str, destination: Path) -> None: ...

    def download_with_info(self, key: str, destination: Path) -> ObjectInfo:
        """Download once and return the metadata carried by that same response."""
        ...

    def open_stream(self, key: str) -> tuple[int, BinaryIO]:
        """``(content_length, reader)`` for one whole-object read.

        Returned rather than downloaded to a path so a verifier can stream tens of
        GB through a digest without ever holding an object on local disk.
        """
        ...

    def head_proof(self, key: str) -> dict[str, Any] | None:
        """Everything one metadata request can say about the STORED bytes.

        ``{"bytes", "content_md5", "sha256_metadata"}``, or None if the object is
        definitively absent. ``content_md5`` is computed BY THE STORE over the bytes
        it holds -- the single-part ETag on S3/R2, the file's own digest on the
        filesystem -- which is what makes it evidence rather than an echo of what the
        client claimed. ``sha256_metadata`` IS such an echo and is labelled so.

        This exists so whole-slice verification does not have to re-download the
        slice. See ``construction_v1_remote.verify_whole_slice_once``.
        """
        ...


def _validate_list_page(
    payload: dict[str, Any],
    prefix: str,
    keys: list[str],
    seen_keys: set[str],
    seen_tokens: set[str],
) -> str | None:
    """Validate one ListObjectsV2 page, append its keys, return the next token.

    Shared by both S3 backends deliberately. These are fail-closed rules -- a page
    that escaped its prefix, repeated a key, or claimed truncation without a fresh
    token is a broken listing, and a broken listing feeds
    ``verify_whole_slice_once``'s exact-set equality. Two copies of them would be
    two chances for one copy to rot.
    """
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
        return None
    if not isinstance(token, str) or not token or token in seen_tokens:
        raise ValueError("R2 truncated list page has no fresh continuation token")
    seen_tokens.add(token)
    return token


def _require_relative_prefix(prefix: str) -> None:
    if not isinstance(prefix, str) or not prefix or prefix.startswith("/"):
        raise ValueError("R2 list prefix must be a non-empty relative key prefix")


def single_part_etag_md5(key: str, etag: Any) -> str:
    """The MD5 an S3/R2 ETag states about a SINGLE-PART object's stored bytes.

    Fails closed on a multipart ETag. A multipart ETag is the digest of the part
    digests plus ``-<part count>``, so it is NOT the MD5 of the content -- comparing
    it to a content MD5 would never match, and comparing it to nothing would be a
    verification that silently stopped verifying. Everything this module publishes
    goes through ``put_object``, which is always single-part (``upload_fileobj`` on
    the s3transfer manager is what multiparts, and is deliberately not used), so a
    dashed ETag here means the publication path changed and the check must be
    revisited rather than skipped.
    """
    if not isinstance(etag, str) or not etag:
        raise RuntimeError(f"object {key} has no ETag to verify its stored bytes")
    digest = etag.strip().strip('"')
    if "-" in digest:
        raise RuntimeError(
            f"object {key} has a MULTIPART ETag ({etag}), which is not the MD5 of "
            "its content. Whole-slice verification compares the ETag to a content "
            "digest, so a multipart publication needs an explicit verification "
            "strategy rather than a comparison that cannot pass."
        )
    if len(digest) != 32 or any(character not in "0123456789abcdef" for character in digest):
        raise RuntimeError(f"object {key} has a non-MD5 ETag ({etag})")
    return digest


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
        with source.open("rb") as handle:
            self.upload_fileobj(handle, key, sha256, size=source.stat().st_size)

    def upload_fileobj(
        self, source: BinaryIO, key: str, sha256: str, *, size: int
    ) -> None:
        destination = self._path(key)
        if destination.exists():
            raise FileExistsError(f"refusing to overwrite object: {key}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=destination.parent, delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
            shutil.copyfileobj(source, temporary)
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

    def download_with_info(self, key: str, destination: Path) -> ObjectInfo:
        source = self._path(key)
        if not source.is_file():
            raise FileNotFoundError(key)
        shutil.copyfile(source, destination)
        metadata_path = source.with_name(f"{source.name}.metadata.json")
        metadata = (
            json.loads(metadata_path.read_text()) if metadata_path.exists() else {}
        )
        return ObjectInfo(
            source.stat().st_size, metadata.get(SHA_METADATA_KEY)
        )

    def download(self, key: str, destination: Path) -> None:
        self.download_with_info(key, destination)

    def open_stream(self, key: str) -> tuple[int, BinaryIO]:
        source = self._path(key)
        if not source.is_file():
            raise FileNotFoundError(key)
        return source.stat().st_size, source.open("rb")

    def head_proof(self, key: str) -> dict[str, Any] | None:
        path = self._path(key)
        if not path.is_file():
            return None
        info = self.head(key)
        assert info is not None
        # Digested from the STORED file, which is this backend's honest analogue of
        # the single-part ETag: a value the store computes over its own bytes rather
        # than one the client asserted. It costs a local read, which is the point --
        # the offline tests then exercise the same verification path R2 takes.
        digest = hashlib.md5(usedforsecurity=False)
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return {
            "bytes": info.bytes,
            "content_md5": digest.hexdigest(),
            "sha256_metadata": info.sha256,
        }


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
        _require_relative_prefix(prefix)
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
                str(LIST_PAGE_KEYS),
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
            continuation = _validate_list_page(
                payload, prefix, keys, seen_keys, seen_tokens
            )
            if continuation is None:
                break
        return sorted(keys)

    def open_stream(self, key: str) -> tuple[int, BinaryIO]:
        """One whole-object read, via a temporary file the reader owns.

        ``aws s3api get-object`` writes to a path, so unlike ``Boto3Store`` this
        cannot hand back a live socket. The temporary is unlinked as soon as it is
        opened, so closing the reader releases the space even on an abort.
        """
        with tempfile.NamedTemporaryFile(
            prefix=".s3store-stream.", delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
        try:
            self.download(key, temporary_path)
            size = temporary_path.stat().st_size
            handle = temporary_path.open("rb")
        finally:
            temporary_path.unlink(missing_ok=True)
        return size, handle

    def upload_fileobj(
        self, source: BinaryIO, key: str, sha256: str, *, size: int
    ) -> None:
        with tempfile.NamedTemporaryFile(
            prefix=".s3store-upload.", delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
            shutil.copyfileobj(source, temporary)
        try:
            self.upload(temporary_path, key, sha256)
        finally:
            temporary_path.unlink(missing_ok=True)

    def head_proof(self, key: str) -> dict[str, Any] | None:
        command = [
            "aws", "s3api", "head-object", "--bucket", self.bucket, "--key", key,
            "--endpoint-url", self.endpoint_url, "--region", "auto",
            "--output", "json",
        ]
        result = subprocess.run(command, text=True, capture_output=True)
        if result.returncode != 0:
            combined = f"{result.stdout}\n{result.stderr}"
            if "404" in combined or "Not Found" in combined or "NoSuchKey" in combined:
                return None
            raise RuntimeError(f"head-object failed for {key}: {result.stderr.strip()}")
        payload = json.loads(result.stdout)
        metadata = payload.get("Metadata") or {}
        return {
            "bytes": int(payload["ContentLength"]),
            "content_md5": single_part_etag_md5(key, payload.get("ETag")),
            "sha256_metadata": metadata.get(SHA_METADATA_KEY),
        }

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

    def download_with_info(self, key: str, destination: Path) -> ObjectInfo:
        try:
            result = self._run(
                [
                    "get-object",
                    "--bucket",
                    self.bucket,
                    "--key",
                    key,
                    "--output",
                    "json",
                    str(destination),
                ],
                capture=True,
            )
        except subprocess.CalledProcessError as error:
            combined = f"{error.stdout or ''}\n{error.stderr or ''}"
            if (
                "404" in combined
                or "Not Found" in combined
                or "NoSuchKey" in combined
            ):
                raise FileNotFoundError(key) from error
            raise RuntimeError(
                f"get-object failed for {key}: {combined.strip()}"
            ) from error
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ValueError("R2 get-object response is not JSON") from exc
        metadata = payload.get("Metadata") or {}
        return ObjectInfo(
            int(payload["ContentLength"]), metadata.get(SHA_METADATA_KEY)
        )

    def download(self, key: str, destination: Path) -> None:
        self.download_with_info(key, destination)


# Connection-pool size for the persistent client. It must be at least the
# publisher's worker count (construction_v1_remote.PUBLISH_CONCURRENCY) or threads
# block on the pool instead of on the network, which silently caps throughput at
# the pool size and looks like R2 being slow.
DEFAULT_MAX_POOL_CONNECTIONS = 32
# Retries for a transient 5xx/throttle. `standard` mode, unlike `legacy`, retries
# on the modelled throttling and transient errors and honours a retry budget, so a
# 65,751-object publication does not abort on one blip. It is NOT a substitute for
# create-only: a retried PUT that already landed comes back 412 and takes the
# byte-exactness path, which is the same path a resumed finalize takes.
MAX_ATTEMPTS = 5

_ABSENT_ERROR_CODES = frozenset({"404", "NoSuchKey", "NotFound"})
_CONFLICT_ERROR_CODES = frozenset({"412", "PreconditionFailed"})


class Boto3Store:
    """Persistent-client S3/R2 adapter: ``S3Store``'s discipline without the process.

    Why this exists rather than just using ``S3Store``. ``aws`` v2 costs 0.339 s of
    CPU per invocation before it does any work -- measured on this repo's dev host,
    ten runs of ``aws --version``: wall 3.39 s, user 3.11 s + sys 0.28 s. It is CPU,
    not latency, so concurrency cannot amortize it past the runner's vCPU count.
    Finalize charges 2 remote operations per published object, so a planet address
    slice at 65,751 objects is 131,502 invocations:

        serial                    131,502 x 0.339 s = 12.4 hours
        4 concurrent, 4 vCPU      131,502 x 0.339 s / 4 = 3.1 hours

    against a 360-minute job timeout, and that is before a byte moves. A persistent
    client removes the term completely; what is left is network latency, which
    concurrency DOES amortize.

    Everything else is deliberately identical to ``S3Store``: create-only through
    ``If-None-Match: '*'``, the sha256 recorded as object metadata, and the same
    ``_validate_list_page`` on every listing page.
    """

    def __init__(
        self,
        bucket: str,
        endpoint_url: str,
        *,
        max_pool_connections: int = DEFAULT_MAX_POOL_CONNECTIONS,
    ):
        try:
            import boto3
            from botocore.config import Config
            from botocore.exceptions import ClientError, HTTPClientError
        except ImportError as error:  # pragma: no cover - covered by the pin test
            raise SystemExit(
                "the construction-v1 R2 transport needs boto3, which is pinned in "
                ".github/requirements-hosted-rowgroup.txt. Refusing to fall back to "
                "the aws CLI: at 0.339 s of CPU per invocation a planet finalize is "
                "12.4 hours of process startup, which is the blocker this backend "
                "exists to remove."
            ) from error
        self._client_error = ClientError
        # Botocore retries failures that occur while obtaining the GET response,
        # but a StreamingBody read happens after that retry loop has returned.
        # A mid-body timeout therefore needs its own bounded whole-GET retry.
        self._stream_retry_error = HTTPClientError
        self.bucket = bucket
        self.endpoint_url = endpoint_url
        # One client, shared by every publisher thread. botocore clients are
        # thread-safe for these calls; the connection pool is what makes them
        # concurrent, hence max_pool_connections above.
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name="auto",
            config=Config(
                retries={"max_attempts": MAX_ATTEMPTS, "mode": "standard"},
                max_pool_connections=max_pool_connections,
                # PLAIN single PUTs, no flexible-checksum framing. Both of these are
                # load-bearing and neither is a preference.
                #
                # botocore's default is `when_supported`, which adds a CRC32 in an
                # `aws-chunked` trailer -- and the trailer location is chosen only for
                # HTTPS, which every real endpoint is. That has two consequences:
                #
                #  1. it wraps the body in `AwsChunkedWrapper` and calls
                #     `botocore.utils.determine_content_length`, which probes the body
                #     with `seek(0, 2)`. MEASURED against the pinned botocore 1.43.56
                #     over an https endpoint: PUTs of 1 KiB and 3 MiB both died before
                #     a byte was sent; only a 0-byte body survived, because seeking to
                #     the end of an empty file lands at 0. So the publication path
                #     could not upload a single non-empty object.
                #  2. it would send `x-amz-checksum-crc32` inside aws-chunked framing
                #     that the aws-cli mirror this replaces never used, and R2's
                #     acceptance of that framing is UNVERIFIED. Sending unverified
                #     framing on every write while rejecting
                #     `x-amz-checksum-sha256` for being unverified would be
                #     incoherent; this makes both choices the same choice.
                #
                # `when_required` sends a checksum only where the S3 model demands one
                # (which PutObject does not), so the request is a plain single PUT and
                # the ETag stays the content MD5 that `single_part_etag_md5` reads.
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )

    def _codes(self, error: Any) -> set[str]:
        response = getattr(error, "response", None) or {}
        metadata = response.get("ResponseMetadata") or {}
        return {
            str((response.get("Error") or {}).get("Code", "")),
            str(metadata.get("HTTPStatusCode", "")),
        }

    def head(self, key: str) -> ObjectInfo | None:
        try:
            payload = self.client.head_object(Bucket=self.bucket, Key=key)
        except self._client_error as error:
            # Only a DEFINITIVE absence is absence. Any other failure raises, so a
            # flaky transport can never read as "not published yet" -- the same
            # fail-closed direction as S3Store.head and
            # construction_v1_hosted._remote_marker_completed.
            if self._codes(error) & _ABSENT_ERROR_CODES:
                return None
            raise RuntimeError(f"head-object failed for {key}: {error}") from error
        metadata = payload.get("Metadata") or {}
        return ObjectInfo(int(payload["ContentLength"]), metadata.get(SHA_METADATA_KEY))

    def list_prefix(self, prefix: str) -> list[str]:
        _require_relative_prefix(prefix)
        keys: list[str] = []
        seen_keys: set[str] = set()
        seen_tokens: set[str] = set()
        continuation: str | None = None
        while True:
            arguments: dict[str, Any] = {
                "Bucket": self.bucket,
                "Prefix": prefix,
                "MaxKeys": LIST_PAGE_KEYS,
            }
            if continuation is not None:
                arguments["ContinuationToken"] = continuation
            continuation = _validate_list_page(
                self.client.list_objects_v2(**arguments),
                prefix,
                keys,
                seen_keys,
                seen_tokens,
            )
            if continuation is None:
                break
        return sorted(keys)

    def upload(self, source: Path, key: str, sha256: str) -> None:
        with source.open("rb") as handle:
            self.upload_fileobj(handle, key, sha256, size=source.stat().st_size)

    def upload_fileobj(
        self, source: BinaryIO, key: str, sha256: str, *, size: int
    ) -> None:
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=source,
                ContentLength=size,
                Metadata={SHA_METADATA_KEY: sha256},
                IfNoneMatch="*",
            )
        except self._client_error as error:
            if self._codes(error) & _CONFLICT_ERROR_CODES:
                raise FileExistsError(
                    f"object appeared during create-only upload: {key}"
                ) from error
            raise RuntimeError(f"put-object failed for {key}: {error}") from error

    def open_stream(self, key: str) -> tuple[int, BinaryIO]:
        try:
            payload = self.client.get_object(Bucket=self.bucket, Key=key)
        except self._client_error as error:
            if self._codes(error) & _ABSENT_ERROR_CODES:
                raise FileNotFoundError(key) from error
            raise RuntimeError(f"get-object failed for {key}: {error}") from error
        return int(payload["ContentLength"]), payload["Body"]

    def head_proof(self, key: str) -> dict[str, Any] | None:
        try:
            payload = self.client.head_object(Bucket=self.bucket, Key=key)
        except self._client_error as error:
            if self._codes(error) & _ABSENT_ERROR_CODES:
                return None
            raise RuntimeError(f"head-object failed for {key}: {error}") from error
        metadata = payload.get("Metadata") or {}
        return {
            "bytes": int(payload["ContentLength"]),
            "content_md5": single_part_etag_md5(key, payload.get("ETag")),
            "sha256_metadata": metadata.get(SHA_METADATA_KEY),
        }

    def download_with_info(self, key: str, destination: Path) -> ObjectInfo:
        last_error: BaseException | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                payload = self.client.get_object(Bucket=self.bucket, Key=key)
                size = int(payload["ContentLength"])
                metadata = payload.get("Metadata") or {}
                body = payload["Body"]
                with contextlib.closing(body), destination.open("wb") as output:
                    shutil.copyfileobj(body, output)
                if destination.stat().st_size != size:
                    raise self._stream_retry_error(
                        error=OSError(
                            f"received {destination.stat().st_size} of {size} bytes"
                        )
                    )
                return ObjectInfo(size, metadata.get(SHA_METADATA_KEY))
            except self._client_error as error:
                if self._codes(error) & _ABSENT_ERROR_CODES:
                    raise FileNotFoundError(key) from error
                last_error = error
            except self._stream_retry_error as error:
                last_error = error
            if attempt < MAX_ATTEMPTS:
                # Small bounded backoff: enough to avoid immediately hitting the
                # same transient connection, negligible next to a large object.
                time.sleep(min(0.25 * (2 ** (attempt - 1)), 2.0))
        raise RuntimeError(
            f"get-object body failed after {MAX_ATTEMPTS} attempts for {key}: "
            f"{last_error}"
        ) from last_error

    def download(self, key: str, destination: Path) -> None:
        self.download_with_info(key, destination)


def s3_object_store(
    bucket: str,
    endpoint_url: str,
    *,
    max_pool_connections: int = DEFAULT_MAX_POOL_CONNECTIONS,
) -> ObjectStore:
    """The S3/R2 backend construction-v1 uses: one persistent client.

    A single producer so no construction phase can end up on the per-invocation
    ``aws`` path by accident. See ``Boto3Store`` for the arithmetic; the short
    version is that ``aws`` v2 startup is 0.339 s of CPU and finalize makes two
    calls per published object.
    """
    if not bucket or not endpoint_url:
        raise ValueError("an S3/R2 object store needs a bucket and an endpoint URL")
    return Boto3Store(
        bucket, endpoint_url, max_pool_connections=max_pool_connections
    )


@contextlib.contextmanager
def streamed_object(store: ObjectStore, key: str) -> Iterator[tuple[int, BinaryIO]]:
    """``open_stream`` as a context manager, so no reader is leaked on an abort."""
    size, handle = store.open_stream(key)
    try:
        yield size, handle
    finally:
        handle.close()


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


def verified_content_addressed_download(
    store: ObjectStore,
    key: str,
    destination: Path,
    *,
    expected_sha256: str,
) -> str:
    """Fetch a content-addressed object with one GET instead of HEAD + GET.

    The GET response already carries both ContentLength and the object's SHA-256
    metadata. The downloaded body is then hashed against the digest encoded in
    its key. A preceding HEAD adds latency and a billed Class B operation but no
    independent evidence: the GET supplies the same metadata and the body supplies
    the stronger byte proof.
    """
    if destination.exists():
        try:
            if sha256_file(destination) == expected_sha256:
                return "local_verified"
        except OSError:
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
        info = store.download_with_info(key, temporary_path)
        if info.sha256 is not None and info.sha256 != expected_sha256:
            raise ValueError(f"staged object metadata digest differs from its key: {key}")
        if temporary_path.stat().st_size != info.bytes:
            raise ValueError(f"artifact byte count differs: {temporary_path}")
        if sha256_file(temporary_path) != expected_sha256:
            raise ValueError(f"artifact SHA-256 differs: {temporary_path}")
        os.replace(temporary_path, destination)
    finally:
        temporary_path.unlink(missing_ok=True)
    return "remote_verified"


def _source_proof(source: BinaryIO) -> dict[str, Any]:
    sha256 = hashlib.sha256()
    content_md5 = hashlib.md5(usedforsecurity=False)
    size = 0
    for chunk in iter(lambda: source.read(1024 * 1024), b""):
        sha256.update(chunk)
        content_md5.update(chunk)
        size += len(chunk)
    return {
        "bytes": size,
        "sha256": sha256.hexdigest(),
        "content_md5": content_md5.hexdigest(),
    }


def _verify_stored_proof(
    key: str, actual: dict[str, Any] | None, expected: dict[str, Any]
) -> None:
    if actual is None:
        raise ValueError(f"uploaded object is absent: {key}")
    if actual != expected:
        raise ValueError(
            f"stored object identity differs; refusing overwrite: {key}"
        )


def ensure_uploaded(store: ObjectStore, source: Path, key: str) -> dict[str, Any]:
    if not source.is_file():
        raise ValueError(f"artifact is not a regular file: {source}")
    with source.open("rb") as handle:
        artifact = _source_proof(handle)
        expected = {
            "bytes": artifact["bytes"],
            "content_md5": artifact["content_md5"],
            "sha256_metadata": artifact["sha256"],
        }
        remote = store.head_proof(key)
        status = "uploaded"
        if remote is None:
            handle.seek(0)
            try:
                store.upload_fileobj(
                    handle,
                    key,
                    artifact["sha256"],
                    size=artifact["bytes"],
                )
            except FileExistsError:
                # A concurrent create won after our HEAD. Never retry with an
                # overwrite: prove the winner's stored bytes exactly as for an
                # object that existed before the operation.
                status = "existing_verified"
            remote = store.head_proof(key)
        else:
            status = "existing_verified"
        _verify_stored_proof(key, remote, expected)
    return {
        "path": str(source),
        "bytes": artifact["bytes"],
        "sha256": artifact["sha256"],
        "key": key,
        "status": status,
        # Compatibility with existing manifests/workflows. Verification now uses
        # the store-computed ETag plus SHA metadata in one HEAD rather than a full
        # GET followed by another HEAD.
        "readback_verified": True,
    }


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
    """Backend for the standalone verified-shuffle CLI.

    Deliberately ``S3Store``, not ``s3_object_store``. This CLI moves a handful of
    objects per invocation (``build-places-region.yml`` uploads one region's shards
    and a family manifest), so per-invocation ``aws`` startup is immaterial here --
    and that workflow installs only duckdb, so requiring boto3 would break it for no
    gain. ``s3_object_store`` stays the only producer construction-v1 uses, where the
    object count is 44,000-66,000 and the startup cost is the blocker.
    """
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
