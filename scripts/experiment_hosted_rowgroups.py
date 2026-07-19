#!/usr/bin/env python3
"""Run a bounded, read-only Overture row-group projection experiment."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import resource
import shutil
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import inventory_address_rowgroups as address_inventory  # noqa: E402


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
        object_url(key),
        method="HEAD",
        headers={"User-Agent": "overture-geocoder-spike/1"},
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
    eligible = [
        item
        for item in eligible
        if item["bytes"] > 0 and item["key"].endswith(".parquet")
    ]
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
            raise ValueError(
                "first row group exceeds the configured byte or row budget"
            )
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


def schema_fingerprint(inventory: dict[str, Any]) -> str | None:
    """Validate a new-style inventory schema contract when one is present.

    Historical measurement reports predate the contract and remain usable by
    the legacy experiment. The global-v2 mapper separately requires this value,
    so a dispatch inventory cannot silently take that compatibility path.
    """
    contract = inventory.get("schema_contract")
    if contract is None:
        return None
    if not isinstance(contract, dict) or set(contract) != {
        "version",
        "fields",
        "fingerprint_sha256",
    }:
        raise ValueError("inventory schema contract fields are invalid")
    expected = contract["fingerprint_sha256"]
    payload = {"version": contract["version"], "fields": contract["fields"]}
    actual = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if not isinstance(expected, str) or actual != expected:
        raise ValueError("inventory schema fingerprint differs from its contract")
    return expected


def canonical_inventory_task(
    inventory: dict[str, Any], *, release: str, task_index: int | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    identity = address_inventory.validate_canonical_inventory(inventory)
    if inventory.get("release") != release:
        raise ValueError("inventory release differs from requested release")
    tasks = identity["tasks"]
    if task_index is None or not 0 <= task_index < len(tasks):
        raise ValueError("inventory task index is outside the plan")
    task = tasks[task_index]
    if task.get("index") != task_index:
        raise ValueError("inventory task index differs from its plan position")
    return identity, task


def exact_task_metadata(
    identity: dict[str, Any], task: dict[str, Any]
) -> dict[bytes, bytes]:
    return {
        address_inventory.INVENTORY_METADATA_KEY: identity["inventory_sha256"].encode(),
        address_inventory.TASK_INDEX_METADATA_KEY: str(task["index"]).encode(),
        address_inventory.TASK_DIGEST_METADATA_KEY: task["task_digest_sha256"].encode(),
        address_inventory.TASK_SOURCE_DIGEST_METADATA_KEY: task[
            "source_digest_sha256"
        ].encode(),
        address_inventory.EXECUTION_BUCKET_METADATA_KEY: task[
            "execution_bucket"
        ].encode(),
    }


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
    filesystem = pafs.S3FileSystem(anonymous=True, region=REGION)
    metadata_started = time.monotonic()
    requested_columns = ADDRESS_COLUMNS if args.family == "addresses" else ()
    columns = list(requested_columns)
    selections: list[dict[str, Any]] = []
    parquets: dict[int, Any] = {}
    sources: dict[int, dict[str, Any]] = {}
    identities_before: dict[int, dict[str, Any]] = {}
    inventory_task: dict[str, Any] | None = None
    source_schema_fingerprint: str | None = None
    address_inventory_digest: str | None = None
    address_task_digest: str | None = None
    address_task_source_digest: str | None = None
    address_execution_bucket: str | None = None
    if args.inventory_report is not None:
        inventory = json.loads(args.inventory_report.read_text())
        inventory_identity, inventory_task = canonical_inventory_task(
            inventory, release=args.release, task_index=args.task_index
        )
        source_schema_fingerprint = inventory_identity["schema_fingerprint_sha256"]
        address_inventory_digest = inventory_identity["inventory_sha256"]
        address_task_digest = inventory_task["task_digest_sha256"]
        address_task_source_digest = inventory_task["source_digest_sha256"]
        address_execution_bucket = inventory_task["execution_bucket"]
        source_inventory = inventory["source_inventory"]
        inventory_objects = source_inventory["objects"]
        object_indexes = {
            source["uri"]: index for index, source in enumerate(inventory_objects)
        }
        for selected_range in inventory_task["ranges"]:
            source_index = object_indexes.get(selected_range["uri"])
            if source_index is None:
                raise ValueError("planned range is absent from the source inventory")
            if source_index not in parquets:
                inventory_source = inventory_objects[source_index]
                key = inventory_source["uri"].removeprefix(f"s3://{BUCKET}/")
                if key == inventory_source["uri"]:
                    raise ValueError(
                        "source inventory URI is outside the Overture bucket"
                    )
                source = {**inventory_source, "key": key}
                identity = head_identity(key)
                if (identity["etag"], identity["bytes"]) != (
                    source["etag"],
                    source["bytes"],
                ):
                    raise ValueError("source identity differs from the inventory")
                source["version_id"] = identity["version_id"]
                parquet = pq.ParquetFile(f"{BUCKET}/{key}", filesystem=filesystem)
                missing_columns = sorted(
                    set(requested_columns) - set(parquet.schema_arrow.names)
                )
                if missing_columns:
                    raise ValueError(
                        f"source schema is missing benchmark columns: {missing_columns}"
                    )
                sources[source_index] = source
                identities_before[source_index] = identity
                parquets[source_index] = parquet
            indexes = list(
                range(
                    selected_range["first_row_group"],
                    selected_range["last_row_group"] + 1,
                )
            )
            if len(indexes) != selected_range["row_groups"]:
                raise ValueError("planned row-group range count differs")
            selections.append(
                {
                    "source_object_index": source_index,
                    "row_group_indexes": indexes,
                }
            )
    else:
        source = discover_object(
            args.release,
            args.family,
            max_keys=args.list_max_keys,
            max_object_bytes=args.max_object_bytes,
        )
        identity = head_identity(source["key"])
        if (identity["etag"], identity["bytes"]) != (
            source["etag"],
            source["bytes"],
        ):
            raise ValueError(
                "source identity changed between listing and pre-read HEAD"
            )
        source["version_id"] = identity["version_id"]
        source_inventory = {
            "schema": "overture-global-source-inventory-v1",
            "release": args.release,
            "family": args.family,
            "objects": [
                {
                    "uri": source["uri"],
                    "etag": source["etag"],
                    "bytes": source["bytes"],
                    "version_id": source["version_id"],
                }
            ],
        }
        parquet = pq.ParquetFile(f"{BUCKET}/{source['key']}", filesystem=filesystem)
        missing_columns = sorted(
            set(requested_columns) - set(parquet.schema_arrow.names)
        )
        if missing_columns:
            raise ValueError(
                f"source schema is missing benchmark columns: {missing_columns}"
            )
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
        sources[0] = source
        identities_before[0] = identity
        parquets[0] = parquet
        selections.append(
            {"source_object_index": 0, "row_group_indexes": row_group_indexes}
        )
    metadata_seconds = time.monotonic() - metadata_started
    source_inventory_json = json.dumps(
        source_inventory, sort_keys=True, separators=(",", ":")
    ).encode()
    source_inventory_digest = hashlib.sha256(source_inventory_json).hexdigest()
    if args.inventory_report is not None and source_inventory_digest != (
        inventory_identity["source_inventory_sha256"]
    ):
        raise ValueError("inventory source digest changed after validation")

    selected_groups = []
    for selection in selections:
        source_index = selection["source_object_index"]
        parquet = parquets[source_index]
        for index in selection["row_group_indexes"]:
            row_group = parquet.metadata.row_group(index)
            selected_compressed_bytes = selected_uncompressed_bytes = 0
            for column_index in range(row_group.num_columns):
                column = row_group.column(column_index)
                if column.path_in_schema.split(".", 1)[0] in columns:
                    selected_compressed_bytes += column.total_compressed_size
                    selected_uncompressed_bytes += column.total_uncompressed_size
            selected_groups.append(
                {
                    "source_object_index": source_index,
                    "index": index,
                    "rows": row_group.num_rows,
                    "rowgroup_uncompressed_bytes": row_group.total_byte_size,
                    "selected_compressed_bytes": selected_compressed_bytes,
                    "selected_uncompressed_bytes": selected_uncompressed_bytes,
                }
            )
    if (
        sum(group["rows"] for group in selected_groups) > args.max_rows
        or len(selected_groups) > args.max_groups
    ):
        raise ValueError("planned task exceeds the configured row or group cap")
    if inventory_task is not None:
        measured = (
            sum(group["rows"] for group in selected_groups),
            sum(group["selected_compressed_bytes"] for group in selected_groups),
            sum(group["selected_uncompressed_bytes"] for group in selected_groups),
        )
        planned = (
            inventory_task["rows"],
            inventory_task["selected_compressed_bytes"],
            inventory_task["selected_uncompressed_bytes"],
        )
        if measured != planned:
            raise ValueError(
                "planned task statistics differ from current Parquet footers"
            )
        if measured[2] > args.target_rowgroup_uncompressed_bytes:
            raise ValueError(
                "planned task exceeds the configured selected-column byte cap"
            )

    read_and_decode_seconds = 0.0
    projection_seconds = 0.0
    tables = []
    for group in selected_groups:
        source_index = group["source_object_index"]
        index = group["index"]
        parquet = parquets[source_index]
        read_started = time.monotonic()
        table = parquet.read_row_group(index, columns=columns, use_threads=True)
        read_and_decode_seconds += time.monotonic() - read_started
        projection_started = time.monotonic()
        row_count = table.num_rows
        table = table.append_column(
            "source_object_index",
            pa.array([source_index] * row_count, type=pa.int32()),
        )
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
        b"overture.release": args.release.encode(),
        b"overture.family": args.family.encode(),
        b"overture.source_inventory_json": source_inventory_json,
    }
    if source_schema_fingerprint is not None:
        artifact_metadata[b"overture.schema_fingerprint_sha256"] = (
            source_schema_fingerprint.encode()
        )
    if inventory_task is not None:
        artifact_metadata.update(
            exact_task_metadata(inventory_identity, inventory_task)
        )
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
    expected_output_columns = columns + [
        "source_object_index",
        "source_row_group",
        "source_row_index",
    ]
    if verification.schema_arrow.names != expected_output_columns:
        raise ValueError("output schema does not match projected schema")
    output_metadata = verification.schema_arrow.metadata or {}
    if (
        output_metadata.get(b"overture.source_inventory_sha256")
        != source_inventory_digest.encode()
    ):
        raise ValueError("output metadata does not bind the source inventory")
    if output_metadata.get(b"overture.source_inventory_json") != source_inventory_json:
        raise ValueError("output metadata does not preserve the source inventory")
    if (
        source_schema_fingerprint is not None
        and output_metadata.get(b"overture.schema_fingerprint_sha256")
        != source_schema_fingerprint.encode()
    ):
        raise ValueError("output metadata does not bind the source schema fingerprint")
    if inventory_task is not None:
        expected_task_metadata = exact_task_metadata(inventory_identity, inventory_task)
        if any(
            output_metadata.get(key) != value
            for key, value in expected_task_metadata.items()
        ):
            raise ValueError("output metadata does not bind the exact inventory task")

    hydration_started = time.monotonic()
    sample_indexes = sorted({0, projected.num_rows // 2, projected.num_rows - 1})
    sample = pq.read_table(
        args.output,
        columns=[
            "id",
            "source_object_index",
            "source_row_group",
            "source_row_index",
        ],
    ).take(pa.array(sample_indexes))
    hydrated_samples = []
    for offset in range(sample.num_rows):
        source_group = sample["source_row_group"][offset].as_py()
        source_index = sample["source_row_index"][offset].as_py()
        source_object_index = sample["source_object_index"][offset].as_py()
        parquet = parquets[source_object_index]
        output_id = sample["id"][offset].as_py()
        hydrated = parquet.read_row_group(source_group, columns=["id"])["id"][
            source_index
        ].as_py()
        if hydrated != output_id:
            raise ValueError("sample locator did not hydrate the expected source ID")
        hydrated_samples.append(
            {
                "output_row": sample_indexes[offset],
                "source_object_index": source_object_index,
                "source_row_group": source_group,
                "source_row_index": source_index,
            }
        )
    hydration_seconds = time.monotonic() - hydration_started
    for source_object_index, source in sources.items():
        identity_after = head_identity(source["key"])
        if identity_after != identities_before[source_object_index]:
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

    disk_after = shutil.disk_usage(args.output.parent)
    report = {
        "schema": "overture-hosted-rowgroup-spike-v2",
        "release": args.release,
        "family": args.family,
        "pyarrow_version": pa.__version__,
        "sources": [
            {
                "source_object_index": source_index,
                **source,
                "parquet_rows": parquets[source_index].metadata.num_rows,
                "parquet_row_groups": parquets[source_index].metadata.num_row_groups,
                "schema_columns": parquets[source_index].schema_arrow.names,
            }
            for source_index, source in sorted(sources.items())
        ],
        "selection": {
            "row_groups": [
                {
                    "source_object_index": group["source_object_index"],
                    "row_group": group["index"],
                }
                for group in selected_groups
            ],
            "row_group_count": len(selected_groups),
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
            "schema_fingerprint_sha256": source_schema_fingerprint,
            "address_inventory_sha256": address_inventory_digest,
            "address_task_index": args.task_index,
            "address_task_digest_sha256": address_task_digest,
            "address_task_source_digest_sha256": address_task_source_digest,
            "address_execution_bucket": address_execution_bucket,
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
            "object_policy": (
                "complete-inventory byte-balanced task"
                if inventory_task is not None
                else "lexicographically first eligible object in the first listing page"
            ),
            "row_group_policy": (
                f"inventory task {args.task_index} with contiguous per-object ranges"
                if inventory_task is not None
                else "contiguous row groups from index zero within fixed limits"
            ),
            "representative": inventory_task is not None,
            "artifact_source_scope": "global inventory with explicit source-object locators",
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
    parser.add_argument("--inventory-report", type=Path)
    parser.add_argument("--task-index", type=int)
    parser.add_argument("--list-max-keys", type=int, default=100)
    parser.add_argument("--max-object-bytes", type=int, default=900_000_000)
    parser.add_argument(
        "--target-rowgroup-uncompressed-bytes", type=int, default=134_217_728
    )
    parser.add_argument("--max-rows", type=int, default=1_500_000)
    parser.add_argument("--max-groups", type=int, default=32)
    parser.add_argument("--max-output-bytes", type=int, default=134_217_728)
    args = parser.parse_args()
    if (args.inventory_report is None) != (args.task_index is None):
        parser.error("--inventory-report and --task-index must be supplied together")
    print(json.dumps(run_experiment(args), sort_keys=True))


if __name__ == "__main__":
    main()
