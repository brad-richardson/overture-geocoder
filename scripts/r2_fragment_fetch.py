#!/usr/bin/env python3
"""Fetch one safe immutable R2 fragment for a bounded global-v2 consumer.

The address planner/reducer invokes this as a no-shell argv adapter and replaces
``{object_key}``/``{output}`` placeholders itself. The adapter only reads under
the supplied execution prefix, writes one explicit temporary path, and leaves
content hash/size validation to the family consumer's pinned manifest.

For address data packs, ``--row-groups`` switches to a fail-closed selective
Parquet path.  PyArrow opens the R2 object as a random-access S3 file, so only
the footer and selected column chunks are read.  The source object's immutable
size/SHA metadata is verified before any projection is admitted, and a
create-only proof sidecar binds the remote footer, original row-group indexes,
    and the locally materialized row groups. The consumer performs the single
    semantic scan while reducing; this transport proof deliberately does not
    decode the materialized rows a second time. There is no whole-file fallback
    for a selective request.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any


SAFE_COMPONENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._=-]{0,255}")
SAFE_BUCKET_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
SELECTIVE_PROOF_SCHEMA = "overture-r2-selective-parquet-v1"
SELECTIVE_PROOF_VERSION = 1


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def head_object_identity(
    *, bucket: str, key: str, endpoint_url: str
) -> dict[str, Any]:
    """Read the create-only R2 identity without downloading object content."""

    command = [
        "aws", "s3api", "head-object", "--bucket", bucket, "--key", key,
        "--endpoint-url", endpoint_url, "--region", "auto", "--output", "json",
        "--no-cli-pager",
    ]
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(f"R2 head-object failed: {result.stderr.strip()}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("R2 head-object result is not JSON") from exc
    metadata = payload.get("Metadata")
    size = payload.get("ContentLength")
    digest = metadata.get("sha256") if isinstance(metadata, dict) else None
    if type(size) is not int or size <= 0 or not isinstance(digest, str):
        raise ValueError("R2 object omits its immutable size/SHA metadata")
    return {"bytes": size, "sha256": digest}


def parse_row_groups(value: str) -> list[int]:
    try:
        selected = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("selected row groups are not JSON") from exc
    if (
        not isinstance(selected, list)
        or not selected
        or selected != sorted(set(selected))
        or any(type(index) is not int or index < 0 for index in selected)
    ):
        raise ValueError("selected row groups must be sorted unique nonnegative integers")
    if json.dumps(selected, separators=(",", ":")) != value:
        raise ValueError("selected row groups are not compact canonical JSON")
    return selected


def _footer_binding(parquet: Any) -> tuple[dict[str, Any], str]:
    metadata = parquet.metadata
    groups = []
    for index in range(metadata.num_row_groups):
        group = metadata.row_group(index)
        groups.append({
            "index": index,
            "records": group.num_rows,
            "total_byte_size": group.total_byte_size,
            "compressed_column_bytes": sum(
                group.column(column).total_compressed_size
                for column in range(group.num_columns)
            ),
            "columns": [
                {
                    "path": group.column(column).path_in_schema,
                    "compressed_bytes": group.column(column).total_compressed_size,
                    "uncompressed_bytes": group.column(column).total_uncompressed_size,
                    "data_page_offset": group.column(column).data_page_offset,
                    "dictionary_page_offset": group.column(column).dictionary_page_offset,
                }
                for column in range(group.num_columns)
            ],
        })
    value = {
        "created_by": metadata.created_by,
        "format_version": metadata.format_version,
        "serialized_size": metadata.serialized_size,
        "records": metadata.num_rows,
        "row_groups": metadata.num_row_groups,
        "columns": metadata.num_columns,
        "schema_sha256": hashlib.sha256(
            str(parquet.schema_arrow.remove_metadata()).encode()
        ).hexdigest(),
        "groups": groups,
    }
    return value, hashlib.sha256(canonical_json(value)).hexdigest()


def _create_only(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
    except FileExistsError:
        raise ValueError(f"selective output already exists: {destination}") from None


def materialize_selected_row_groups(
    *,
    bucket: str,
    prefix: str,
    object_key: str,
    output: Path,
    proof: Path,
    endpoint_url: str,
    row_groups: list[int],
    expected_bytes: int,
    expected_sha256: str,
    filesystem: Any | None = None,
    remote_identity: dict[str, Any] | None = None,
    artifact_family: str = "address",
) -> dict[str, Any]:
    """Materialize selected original row groups and publish their exact proof."""

    if not SAFE_BUCKET_RE.fullmatch(bucket):
        raise ValueError("R2 bucket is invalid")
    if (
        type(expected_bytes) is not int
        or expected_bytes <= 0
        or not SHA256_RE.fullmatch(expected_sha256)
    ):
        raise ValueError("expected whole-object identity is invalid")
    if (
        not row_groups
        or row_groups != sorted(set(row_groups))
        or any(type(index) is not int or index < 0 for index in row_groups)
    ):
        raise ValueError("selected row groups are invalid")
    if output.exists() or proof.exists():
        raise ValueError("selective output/proof already exists")
    key = safe_key(prefix, object_key)
    if remote_identity is None:
        remote_identity = head_object_identity(
            bucket=bucket, key=key, endpoint_url=endpoint_url
        )
    if remote_identity != {"bytes": expected_bytes, "sha256": expected_sha256}:
        raise ValueError("R2 whole-object identity differs from the reduce plan")

    import pyarrow.fs as pafs
    import pyarrow.parquet as pq

    if filesystem is None:
        filesystem = pafs.S3FileSystem(
            region="auto", endpoint_override=endpoint_url,
        )
    filesystem_path = f"{bucket}/{key}"
    info = filesystem.get_file_info(filesystem_path)
    if info.type != pafs.FileType.File or info.size != expected_bytes:
        raise ValueError("R2 random-access object size differs from its identity")

    output.parent.mkdir(parents=True, exist_ok=True)
    proof.parent.mkdir(parents=True, exist_ok=True)
    output_temp: Path | None = None
    proof_temp: Path | None = None
    output_published = False
    proof_published = False
    try:
        with tempfile.NamedTemporaryFile(
            prefix=".selective-parquet-", suffix=".parquet",
            dir=output.parent, delete=False,
        ) as handle:
            output_temp = Path(handle.name)
        with filesystem.open_input_file(filesystem_path) as source:
            parquet = pq.ParquetFile(source, pre_buffer=False)
            if any(index >= parquet.metadata.num_row_groups for index in row_groups):
                raise ValueError("selected row group is outside the remote footer")
            if artifact_family not in {"address", "places"}:
                raise ValueError("selective Parquet artifact family is invalid")
            header_key = (
                b"overture.address_pack_header"
                if artifact_family == "address"
                else b"overture.places_pack_header"
            )
            raw_header = (parquet.schema_arrow.metadata or {}).get(header_key)
            if raw_header is None:
                raise ValueError("remote Parquet pack omits its provenance header")
            try:
                source_header = json.loads(raw_header)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("remote address pack header is invalid") from exc
            if artifact_family == "address" and source_header.get("records") != parquet.metadata.num_rows:
                raise ValueError("remote address pack header/footer records differ")
            footer, footer_sha256 = _footer_binding(parquet)
            if artifact_family == "address":
                from global_v2_address_map import shuffle_schema

                expected_schema = shuffle_schema()
            else:
                from global_v2_places_map import _fragment_arrow_schema

                expected_schema = _fragment_arrow_schema()
            if not parquet.schema_arrow.remove_metadata().equals(expected_schema):
                raise ValueError("remote Parquet pack schema differs")
            footer["schema_sha256"] = hashlib.sha256(
                str(expected_schema).encode()
            ).hexdigest()
            footer_sha256 = hashlib.sha256(canonical_json(footer)).hexdigest()
            selected_records = sum(
                parquet.metadata.row_group(index).num_rows for index in row_groups
            )
            materialized_header = (
                {**source_header, "records": selected_records}
                if artifact_family == "address"
                else source_header
            )
            schema_metadata = dict(parquet.schema_arrow.metadata or {})
            schema_metadata[header_key] = canonical_json(
                materialized_header
            ).rstrip(b"\n")
            writer = pq.ParquetWriter(
                output_temp,
                parquet.schema_arrow.with_metadata(schema_metadata),
                compression="zstd",
                compression_level=6,
                use_dictionary=True,
                write_statistics=True,
                data_page_version="1.0",
                version="2.6",
            )
            try:
                for original_index in row_groups:
                    table = parquet.read_row_group(original_index, use_threads=False)
                    expected_records_for_group = parquet.metadata.row_group(
                        original_index
                    ).num_rows
                    if table.num_rows != expected_records_for_group:
                        raise ValueError("remote selected row-group records differ")
                    writer.write_table(
                        table.replace_schema_metadata(schema_metadata),
                        row_group_size=max(1, table.num_rows),
                    )
            finally:
                writer.close()

        materialized = pq.ParquetFile(output_temp)
        if (
            materialized.metadata.num_row_groups != len(row_groups)
            or materialized.metadata.num_rows != selected_records
        ):
            raise ValueError("materialized Parquet row-group layout differs")
        mapping = []
        for materialized_index, original_index in enumerate(row_groups):
            source_group = parquet.metadata.row_group(original_index)
            materialized_group = materialized.metadata.row_group(materialized_index)
            if materialized_group.num_rows != source_group.num_rows:
                raise ValueError("materialized selected row-group records differ")
            mapping.append({
                "materialized_index": materialized_index,
                "original_index": original_index,
                "records": source_group.num_rows,
                "compressed_column_bytes": sum(
                    source_group.column(column).total_compressed_size
                    for column in range(source_group.num_columns)
                ),
            })
        output_identity = {
            "bytes": output_temp.stat().st_size,
            "sha256": sha256_file(output_temp),
            "row_groups": len(mapping),
            "records": selected_records,
        }
        result = {
            "schema": SELECTIVE_PROOF_SCHEMA,
            "version": SELECTIVE_PROOF_VERSION,
            "object_key": object_key,
            "whole_object": {
                "bytes": expected_bytes,
                "sha256": expected_sha256,
                "metadata_verified": True,
            },
            "selected_original_row_groups": row_groups,
            "source_header": source_header,
            "source_header_sha256": hashlib.sha256(raw_header).hexdigest(),
            "source_footer": {
                "binding_sha256": footer_sha256,
                "serialized_size": footer["serialized_size"],
                "records": footer["records"],
                "row_groups": footer["row_groups"],
                "schema_sha256": footer["schema_sha256"],
            },
            "materialized": output_identity,
            "materialized_row_groups": mapping,
            "transport": {
                "kind": "pyarrow-s3-random-access",
                "whole_object_downloaded": False,
                "whole_object_fallback_allowed": False,
            },
        }
        if artifact_family == "places":
            result["artifact_family"] = "places"
        with tempfile.NamedTemporaryFile(
            prefix=".selective-proof-", suffix=".json",
            dir=proof.parent, delete=False,
        ) as handle:
            proof_temp = Path(handle.name)
            handle.write(canonical_json(result))
        _create_only(output_temp, output)
        output_published = True
        try:
            _create_only(proof_temp, proof)
            proof_published = True
        except BaseException:
            output.unlink(missing_ok=True)
            output_published = False
            raise
        return result
    except BaseException:
        if output_published:
            output.unlink(missing_ok=True)
        if proof_published:
            proof.unlink(missing_ok=True)
        raise
    finally:
        if output_temp is not None:
            output_temp.unlink(missing_ok=True)
        if proof_temp is not None:
            proof_temp.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--object-key", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--endpoint-url")
    parser.add_argument("--row-groups")
    parser.add_argument("--expected-bytes", type=int)
    parser.add_argument("--expected-sha256")
    parser.add_argument("--proof", type=Path)
    parser.add_argument("--artifact-family", choices=("address", "places"), default="address")
    args = parser.parse_args()
    endpoint = args.endpoint_url or os.environ.get("R2_ENDPOINT")
    if endpoint is None:
        account = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
        if account:
            endpoint = f"https://{account}.r2.cloudflarestorage.com"
    if not endpoint:
        raise SystemExit("R2 endpoint is required")
    selective_values = (
        args.row_groups, args.expected_bytes, args.expected_sha256, args.proof
    )
    if any(value is not None for value in selective_values):
        if any(value is None for value in selective_values):
            raise SystemExit("selective fetch requires row groups, identity, and proof")
        materialize_selected_row_groups(
            bucket=args.bucket,
            prefix=args.prefix,
            object_key=args.object_key,
            output=args.output,
            proof=args.proof,
            endpoint_url=endpoint,
            row_groups=parse_row_groups(args.row_groups),
            expected_bytes=args.expected_bytes,
            expected_sha256=args.expected_sha256,
            artifact_family=args.artifact_family,
        )
    else:
        fetch(
            bucket=args.bucket,
            prefix=args.prefix,
            object_key=args.object_key,
            output=args.output,
            endpoint_url=endpoint,
        )


if __name__ == "__main__":
    main()
