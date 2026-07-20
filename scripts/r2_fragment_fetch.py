#!/usr/bin/env python3
"""Fetch one safe immutable R2 fragment for a bounded global-v2 consumer.

The address planner/reducer invokes this as a no-shell argv adapter and replaces
``{object_key}``/``{output}`` placeholders itself. The adapter only reads under
the supplied execution prefix, writes one explicit temporary path, and leaves
content hash/size validation to the family consumer's pinned manifest.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
from pathlib import Path, PurePosixPath


SAFE_COMPONENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._=-]{0,255}")
SAFE_BUCKET_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}")


def safe_key(prefix: str, object_key: str) -> str:
    # Validate the spelling before constructing a PurePosixPath: pathlib
    # normalizes both empty and ``.`` components, which would otherwise turn
    # non-canonical attacker-controlled keys such as ``a//b`` or ``a/./b``
    # into an apparently safe ``a/b``.
    prefix_text = prefix.strip("/")
    prefix_parts = tuple(prefix_text.split("/"))
    object_parts = tuple(object_key.split("/"))
    if (
        not prefix_parts
        or not object_parts
        or PurePosixPath(object_key).is_absolute()
        or any(
            part in {"", ".", ".."} or not SAFE_COMPONENT_RE.fullmatch(part)
            for part in (*prefix_parts, *object_parts)
        )
    ):
        raise ValueError("R2 fragment key is not canonical and prefix-contained")
    return PurePosixPath(*prefix_parts, *object_parts).as_posix()


def fetch(
    *,
    bucket: str,
    prefix: str,
    object_key: str,
    output: Path,
    endpoint_url: str,
) -> None:
    if not SAFE_BUCKET_RE.fullmatch(bucket):
        raise ValueError("R2 bucket is invalid")
    key = safe_key(prefix, object_key)
    if output.exists():
        raise ValueError("R2 fragment output already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "aws",
        "s3api",
        "get-object",
        "--bucket",
        bucket,
        "--key",
        key,
        str(output),
        "--endpoint-url",
        endpoint_url,
        "--region",
        "auto",
        "--no-cli-pager",
    ]
    try:
        subprocess.run(command, check=True)
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    if not output.is_file() or output.stat().st_size <= 0:
        output.unlink(missing_ok=True)
        raise ValueError("R2 fragment fetch produced no bytes")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--object-key", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--endpoint-url")
    args = parser.parse_args()
    endpoint = args.endpoint_url or os.environ.get("R2_ENDPOINT")
    if endpoint is None:
        account = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
        if account:
            endpoint = f"https://{account}.r2.cloudflarestorage.com"
    if not endpoint:
        raise SystemExit("R2 endpoint is required")
    fetch(
        bucket=args.bucket,
        prefix=args.prefix,
        object_key=args.object_key,
        output=args.output,
        endpoint_url=endpoint,
    )


if __name__ == "__main__":
    main()
