#!/usr/bin/env python3
"""Run a bounded, read-only Overture row-group projection experiment."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import resource
import shutil
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


BUCKET = "overturemaps-us-west-2"
REGION = "us-west-2"
FAMILY_PATHS = {"addresses": ("addresses", "address")}
ADDRESS_COLUMNS = (
    "id",
    "street",
    "number",
    "unit",
    "postcode",
    "postal_city",
    "address_levels",
    "country",
    "geometry",
)


def list_url(prefix: str, max_keys: int) -> str:
    query = urllib.parse.urlencode(
        {"list-type": "2", "prefix": prefix, "max-keys": str(max_keys)}
    )
    return f"https://{BUCKET}.s3.{REGION}.amazonaws.com/?{query}"


def parse_listing(payload: bytes) -> list[dict[str, Any]]:
    root = ET.fromstring(payload)
    namespace = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
    objects = []
    for item in root.findall("s3:Contents", namespace):
        key = item.findtext("s3:Key", namespaces=namespace)
        etag = item.findtext("s3:ETag", namespaces=namespace)
        size = item.findtext("s3:Size", namespaces=namespace)
        if key is None or etag is None or size is None:
            raise ValueError("S3 listing object is missing key, ETag, or size")
        objects.append({"key": key, "etag": etag.strip('"'), "bytes": int(size)})
    return objects


def object_url(key: str) -> str:
    return f"https://{BUCKET}.s3.{REGION}.amazonaws.com/{urllib.parse.quote(key)}"


def head_identity(key: str) -> dict[str, Any]:
    request = urllib.request.Request(
        object_url(key), method="HEAD", headers={"User-Agent": "overture-geocoder-spike/1"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return {
            "etag": response.headers["ETag"].strip('"'),
            "bytes": int(response.headers["Content-Length"]),
            "version_id": response.headers.get("x-amz-version-id"),
        }


def parse_network_received(payload: str) -> int:
    total = 0
    for line in payload.splitlines()[2:]:
        if ":" not in line:
            continue
        interface, counters = line.split(":", 1)
        if interface.strip() == "lo":
            continue
        fields = counters.split()
        if fields:
            total += int(fields[0])
    return total


def network_received_bytes() -> int | None:
    path = Path("/proc/net/dev")
    return parse_network_received(path.read_text()) if path.exists() else None


class BoundedWriter(io.RawIOBase):
    def __init__(self, path: Path, limit: int):
        self._file = path.open("wb")
        self._limit = limit

    def writable(self) -> bool:
        return True

    def write(self, data: bytes) -> int:
        if self._file.tell() + len(data) > self._limit:
            raise ValueError(f"Parquet output exceeds {self._limit} byte hard limit")
        return self._file.write(data)

    def tell(self) -> int:
        return self._file.tell()

    def flush(self) -> None:
        self._file.flush()

    def close(self) -> None:
        if not self.closed:
            super().close()
            self._file.close()


def discover_object(
    release: str, family: str, *, max_keys: int, max_object_bytes: int
) -> dict[str, Any]:
    theme, feature_type = FAMILY_PATHS[family]
    prefix = f"release/{release}/theme={theme}/type={feature_type}/"
    request = urllib.request.Request(
        list_url(prefix, max_keys), headers={"User-Agent": "overture-geocoder-spike/1"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        objects = parse_listing(response.read())
    eligible = [item for item in objects if item["bytes"] <= max_object_bytes]
    eligible = [item for item in eligible if item["bytes"] > 0 and item["key"].endswith(".parquet")]
    if not eligible:
        raise ValueError(
            f"no object under {max_object_bytes} bytes in first {len(objects)} listing entries"
        )
    selected = sorted(eligible, key=lambda item: item["key"])[0]
    selected["uri"] = f"s3://{BUCKET}/{selected['key']}"
    return selected


def select_row_groups(
    groups: list[dict[str, int]],
    *,
    target_rowgroup_uncompressed_bytes: int,
    max_rows: int,
    max_groups: int,
) -> list[int]:
    selected: list[int] = []
    byte_count = 0
    row_count = 0
    for group in groups:
        next_bytes = byte_count + group["rowgroup_uncompressed_bytes"]
        next_rows = row_count + group["rows"]
        if selected and (
            next_bytes > target_rowgroup_uncompressed_bytes
            or next_rows > max_rows
            or len(selected) >= max_groups
        ):
            break
        if not selected and (
            group["rowgroup_uncompressed_bytes"] > target_rowgroup_uncompressed_bytes
            or group["rows"] > max_rows
        ):
            raise ValueError("first row group exceeds the configured byte or row budget")
        selected.append(group["index"])
        byte_count = next_bytes
        row_count = next_rows
    if not selected:
        raise ValueError("no row groups selected")
    return selected


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def peak_rss_bytes() -> int:
    # Linux reports KiB; macOS reports bytes. The hosted experiment runs Linux.
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value * 1024 if value < 10_000_000 else value


def run_experiment(args: argparse.Namespace) -> dict[str, Any]:
    try:
        import pyarrow as pa
        import pyarrow.fs as pafs
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover - exercised by hosted workflow
        raise SystemExit("experiment_hosted_rowgroups.py requires pyarrow") from exc

    started = time.monotonic()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    disk_before = shutil.disk_usage(args.output.parent)
    network_before = network_received_bytes()
    source = discover_object(
        args.release,
        args.family,
        max_keys=args.list_max_keys,
        max_object_bytes=args.max_object_bytes,
    )
    identity_before = head_identity(source["key"])
    if (identity_before["etag"], identity_before["bytes"]) != (
        source["etag"],
        source["bytes"],
    ):
        raise ValueError("source identity changed between listing and pre-read HEAD")
    source["version_id"] = identity_before["version_id"]
    source_inventory = {
        "release": args.release,
        "family": args.family,
        "uri": source["uri"],
        "etag": source["etag"],
        "bytes": source["bytes"],
        "version_id": source["version_id"],
    }
    source_inventory_digest = hashlib.sha256(
        json.dumps(source_inventory, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    filesystem = pafs.S3FileSystem(anonymous=True, region=REGION)
    s3_path = f"{BUCKET}/{source['key']}"
    metadata_started = time.monotonic()
    parquet = pq.ParquetFile(s3_path, filesystem=filesystem)
    metadata_seconds = time.monotonic() - metadata_started
    available_columns = set(parquet.schema_arrow.names)
    requested_columns = ADDRESS_COLUMNS if args.family == "addresses" else ()
    missing_columns = sorted(set(requested_columns) - available_columns)
    if missing_columns:
        raise ValueError(f"source schema is missing benchmark columns: {missing_columns}")
    columns = list(requested_columns)

    groups = []
    for index in range(parquet.metadata.num_row_groups):
        row_group = parquet.metadata.row_group(index)
        selected_compressed_bytes = 0
        selected_uncompressed_bytes = 0
        for column_index in range(row_group.num_columns):
            column = row_group.column(column_index)
            if column.path_in_schema.split(".", 1)[0] in columns:
                selected_compressed_bytes += column.total_compressed_size
                selected_uncompressed_bytes += column.total_uncompressed_size
        groups.append(
            {
                "index": index,
                "rows": row_group.num_rows,
                "rowgroup_uncompressed_bytes": row_group.total_byte_size,
                "selected_compressed_bytes": selected_compressed_bytes,
                "selected_uncompressed_bytes": selected_uncompressed_bytes,
            }
        )
    row_group_indexes = select_row_groups(
        groups,
        target_rowgroup_uncompressed_bytes=args.target_rowgroup_uncompressed_bytes,
        max_rows=args.max_rows,
        max_groups=args.max_groups,
    )

    read_and_decode_seconds = 0.0
    projection_seconds = 0.0
    tables = []
    for index in row_group_indexes:
        read_started = time.monotonic()
        table = parquet.read_row_group(index, columns=columns, use_threads=True)
        read_and_decode_seconds += time.monotonic() - read_started
        projection_started = time.monotonic()
        row_count = table.num_rows
        table = table.append_column(
            "source_row_group", pa.array([index] * row_count, type=pa.int32())
        )
        table = table.append_column(
            "source_row_index", pa.array(range(row_count), type=pa.int32())
        )
        tables.append(table)
        projection_seconds += time.monotonic() - projection_started
    concat_started = time.monotonic()
    projected = pa.concat_tables(tables)
    projection_seconds += time.monotonic() - concat_started
    artifact_metadata = {
        **(projected.schema.metadata or {}),
        b"overture.source_inventory_sha256": source_inventory_digest.encode(),
        b"overture.source_uri": source["uri"].encode(),
        b"overture.source_etag": source["etag"].encode(),
        b"overture.release": args.release.encode(),
        b"overture.family": args.family.encode(),
        b"overture.source_inventory_json": json.dumps(
            source_inventory, sort_keys=True, separators=(",", ":")
        ).encode(),
    }
    projected = projected.replace_schema_metadata(artifact_metadata)
    network_after_projection = network_received_bytes()

    write_started = time.monotonic()
    writer = BoundedWriter(args.output, args.max_output_bytes)
    try:
        pq.write_table(
            projected,
            pa.PythonFile(writer, mode="w"),
            compression="zstd",
            use_dictionary=True,
            row_group_size=128_000,
        )
    finally:
        writer.close()
    write_seconds = time.monotonic() - write_started
    output_bytes = args.output.stat().st_size

    verification = pq.ParquetFile(args.output)
    if verification.metadata.num_rows != projected.num_rows:
        raise ValueError("output record count does not match projected input")
    expected_output_columns = columns + ["source_row_group", "source_row_index"]
    if verification.schema_arrow.names != expected_output_columns:
        raise ValueError("output schema does not match projected schema")
    output_metadata = verification.schema_arrow.metadata or {}
    if output_metadata.get(b"overture.source_inventory_sha256") != source_inventory_digest.encode():
        raise ValueError("output metadata does not bind the source inventory")
    if output_metadata.get(b"overture.source_inventory_json") != json.dumps(
        source_inventory, sort_keys=True, separators=(",", ":")
    ).encode():
        raise ValueError("output metadata does not preserve the source inventory")

    hydration_started = time.monotonic()
    sample_indexes = sorted({0, projected.num_rows // 2, projected.num_rows - 1})
    sample = pq.read_table(
        args.output, columns=["id", "source_row_group", "source_row_index"]
    ).take(pa.array(sample_indexes))
    hydrated_samples = []
    for offset in range(sample.num_rows):
        source_group = sample["source_row_group"][offset].as_py()
        source_index = sample["source_row_index"][offset].as_py()
        output_id = sample["id"][offset].as_py()
        hydrated = parquet.read_row_group(source_group, columns=["id"])["id"][source_index].as_py()
        if hydrated != output_id:
            raise ValueError("sample locator did not hydrate the expected source ID")
        hydrated_samples.append(
            {"output_row": sample_indexes[offset], "source_row_group": source_group, "source_row_index": source_index}
        )
    hydration_seconds = time.monotonic() - hydration_started
    identity_after = head_identity(source["key"])
    if identity_after != identity_before:
        raise ValueError("source identity changed during the experiment")
    network_after_hydration = network_received_bytes()
    initial_network_received_delta = (
        network_after_projection - network_before
        if network_before is not None and network_after_projection is not None
        else None
    )
    hydration_network_received_delta = (
        network_after_hydration - network_after_projection
        if network_after_projection is not None and network_after_hydration is not None
        else None
    )
    total_network_received_delta = (
        network_after_hydration - network_before
        if network_before is not None and network_after_hydration is not None
        else None
    )

    selected_groups = [groups[index] for index in row_group_indexes]
    disk_after = shutil.disk_usage(args.output.parent)
    report = {
        "schema": "overture-hosted-rowgroup-spike-v1",
        "release": args.release,
        "family": args.family,
        "pyarrow_version": pa.__version__,
        "source": {
            **source,
            "parquet_rows": parquet.metadata.num_rows,
            "parquet_row_groups": parquet.metadata.num_row_groups,
            "schema_columns": parquet.schema_arrow.names,
        },
        "selection": {
            "row_groups": row_group_indexes,
            "row_group_count": len(row_group_indexes),
            "rows": projected.num_rows,
            "rowgroup_uncompressed_bytes": sum(
                item["rowgroup_uncompressed_bytes"] for item in selected_groups
            ),
            "selected_uncompressed_bytes": sum(
                item["selected_uncompressed_bytes"] for item in selected_groups
            ),
            "estimated_selected_compressed_bytes": sum(
                item["selected_compressed_bytes"] for item in selected_groups
            ),
            "projected_columns": columns,
        },
        "output": {
            "path": str(args.output),
            "bytes": output_bytes,
            "sha256": sha256_file(args.output),
            "rows": verification.metadata.num_rows,
            "row_groups": verification.metadata.num_row_groups,
            "columns": verification.schema_arrow.names,
            "source_inventory_sha256": source_inventory_digest,
        },
        "verification": {
            "pre_post_source_identity_match": True,
            "artifact_source_metadata_match": True,
            "output_record_count_match": True,
            "hydrated_sample_count": len(hydrated_samples),
            "hydrated_samples": hydrated_samples,
        },
        "resources": {
            "metadata_seconds": metadata_seconds,
            "read_and_decode_seconds": read_and_decode_seconds,
            "projection_seconds": projection_seconds,
            "hydration_verification_seconds": hydration_seconds,
            "write_seconds": write_seconds,
            "elapsed_seconds": time.monotonic() - started,
            "peak_rss_bytes": peak_rss_bytes(),
            "initial_network_received_bytes_upper_bound": initial_network_received_delta,
            "hydration_network_received_bytes_upper_bound": hydration_network_received_delta,
            "total_network_received_bytes_upper_bound": total_network_received_delta,
            "network_measurement_scope": (
                "Linux runner non-loopback interface delta; includes unrelated runner traffic"
                if total_network_received_delta is not None
                else "unavailable outside Linux /proc"
            ),
            "disk_free_before": disk_before.free,
            "disk_free_after": disk_after.free,
        },
        "limits": {
            "max_object_bytes": args.max_object_bytes,
            "target_rowgroup_uncompressed_bytes": args.target_rowgroup_uncompressed_bytes,
            "max_rows": args.max_rows,
            "max_groups": args.max_groups,
            "max_output_bytes": args.max_output_bytes,
        },
        "sampling": {
            "object_policy": "lexicographically first eligible object in the first listing page",
            "row_group_policy": "contiguous row groups from index zero within fixed limits",
            "representative": False,
            "artifact_source_scope": "one source object; global fragments require a source dictionary",
        },
    }
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release", required=True)
    parser.add_argument("--family", choices=sorted(FAMILY_PATHS), default="addresses")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--json-out", type=Path, required=True)
    parser.add_argument("--list-max-keys", type=int, default=100)
    parser.add_argument("--max-object-bytes", type=int, default=900_000_000)
    parser.add_argument("--target-rowgroup-uncompressed-bytes", type=int, default=134_217_728)
    parser.add_argument("--max-rows", type=int, default=1_500_000)
    parser.add_argument("--max-groups", type=int, default=32)
    parser.add_argument("--max-output-bytes", type=int, default=134_217_728)
    args = parser.parse_args()
    print(json.dumps(run_experiment(args), sort_keys=True))


if __name__ == "__main__":
    main()
