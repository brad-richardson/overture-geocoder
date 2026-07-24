#!/usr/bin/env python3
"""Bounded columnar projection for one frozen Places inventory task.

This is a narrow adapter over the existing Places inventory and public S3
reader. It never constructs Python feature rows, dictionaries, or tuples.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import resource
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import global_v2_places_inventory as inventory  # noqa: E402
from experiment_hosted_rowgroups import (  # noqa: E402
    BoundedWriter,
    network_received_bytes,
    urlopen_with_retry,
)


SCHEMA = "overture-places-construction-v1-projection-report-v1"
PROJECTION_SCHEMA = "overture-places-construction-v1-physical-arrow-v1"
MAXIMUM_BATCH_ROWS = 65_536


def measured_network_received_bytes() -> int | None:
    value = network_received_bytes()
    if value is not None:
        return value
    if sys.platform != "darwin":
        return None
    try:
        payload = subprocess.check_output(
            ["netstat", "-ibn"], text=True, stderr=subprocess.DEVNULL
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    lines = [line.split() for line in payload.splitlines() if line.split()]
    if not lines or "Name" not in lines[0] or "Ibytes" not in lines[0]:
        return None
    name_index = lines[0].index("Name")
    bytes_index = lines[0].index("Ibytes")
    interfaces: dict[str, int] = {}
    for fields in lines[1:]:
        if len(fields) <= max(name_index, bytes_index):
            continue
        name = fields[name_index]
        if name.startswith("lo") or name in interfaces:
            continue
        try:
            interfaces[name] = int(fields[bytes_index])
        except ValueError:
            continue
    return sum(interfaces.values()) if interfaces else None


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def head_identity(uri: str) -> dict[str, Any]:
    bucket, separator, key = uri.removeprefix("s3://").partition("/")
    if not separator or not bucket or not key:
        raise ValueError("Places source URI is invalid")
    url = f"https://{bucket}.s3.{inventory.REGION}.amazonaws.com/{urllib.parse.quote(key, safe='/')}"
    request = urllib.request.Request(
        url,
        method="HEAD",
        headers={"User-Agent": "overture-geocoder-places-construction-v1/1"},
    )
    # P1-3: retry the read-only HEAD with bounded exponential backoff; fail
    # closed (never treat a transient fault as a changed/absent object).
    _body, headers = urlopen_with_retry(request, timeout=60)
    etag = headers.get("ETag")
    size = headers.get("Content-Length")
    if etag is None or size is None:
        raise ValueError("Places source identity response is incomplete")
    return {"etag": etag.strip('"'), "bytes": int(size)}


def _struct_field(array: Any, *names: str) -> Any:
    import pyarrow.compute as pc

    value = array
    for name in names:
        value = pc.struct_field(value, name)
    return value


def _text(array: Any) -> Any:
    import pyarrow as pa
    import pyarrow.compute as pc

    return pc.fill_null(pc.cast(array, pa.string()), "")


def _first_address_field(addresses: Any, name: str) -> Any:
    import numpy as np
    import pyarrow as pa
    import pyarrow.compute as pc

    offsets = addresses.offsets.to_numpy(zero_copy_only=False)
    valid = np.logical_and(np.asarray(addresses.is_valid()), offsets[1:] > offsets[:-1])
    indexes = pa.array(offsets[:-1], mask=np.logical_not(valid), type=pa.int64())
    values = _struct_field(addresses.values, name)
    selected = pc.take(values, indexes)
    return pc.if_else(pa.array(valid), _text(selected), pa.scalar(""))


def flatten_batch(
    batch: Any, *, object_index: int, row_group: int, row_offset: int
) -> Any:
    """Flatten one nested source batch using Arrow/NumPy column operations only."""
    import numpy as np
    import pyarrow as pa
    import pyarrow.compute as pc

    names = batch.column(batch.schema.get_field_index("names"))
    common = _struct_field(names, "common")
    common_names = pa.ListArray.from_arrays(
        common.offsets, common.items, mask=common.is_null()
    )
    category = _text(_struct_field(batch["categories"], "primary"))
    basic_category = _text(batch["basic_category"])
    category = pc.if_else(pc.not_equal(category, ""), category, basic_category)
    rows = batch.num_rows
    return pa.RecordBatch.from_arrays(
        [
            pc.cast(batch["id"], pa.string()),
            _text(_struct_field(names, "primary")),
            common_names,
            _text(_struct_field(batch["brand"], "names", "primary")),
            category,
            _first_address_field(batch["addresses"], "locality"),
            _first_address_field(batch["addresses"], "region"),
            _first_address_field(batch["addresses"], "country"),
            pc.cast(batch["confidence"], pa.float64()),
            _text(batch["operating_status"]),
            pc.cast(batch["geometry"], pa.binary()),
            pa.array(np.full(rows, object_index, dtype=np.int32)),
            pa.array(np.full(rows, row_group, dtype=np.int32)),
            pa.array(np.arange(row_offset, row_offset + rows, dtype=np.int32)),
        ],
        names=[
            "id",
            "primary_name",
            "common_names",
            "brand_name",
            "category",
            "locality",
            "region",
            "country",
            "confidence",
            "operating_status",
            "geometry",
            "source_object_index",
            "source_row_group",
            "source_row_index",
        ],
    )


def projection_identity(
    value: dict[str, Any],
    task: dict[str, Any],
    inventory_file_sha256: str,
    spec_sha256: str,
) -> dict[str, Any]:
    selected_objects = sorted({item["object_index"] for item in task["ranges"]})
    return {
        "schema": PROJECTION_SCHEMA,
        "release": value["release"],
        "inventory_sha256": value["inventory_sha256"],
        "inventory_file_sha256": inventory_file_sha256,
        "schema_fingerprint_sha256": value["schema_contract"]["fingerprint_sha256"],
        "evidence_spec_sha256": spec_sha256,
        "task_index": task["index"],
        "task_digest": task["task_digest"],
        "task_source_digest": task["source_digest"],
        "expected_input_records": task["expected_input_records"],
        "selected_uncompressed_bytes": task["selected_uncompressed_bytes"],
        "selected_compressed_bytes": sum(
            item["selected_compressed_bytes"] for item in task["ranges"]
        ),
        "row_groups": task["row_groups"],
        "ranges": task["ranges"],
        "objects": [
            {
                key: value["objects"][index][key]
                for key in (
                    "uri",
                    "etag",
                    "bytes",
                    "records",
                    "row_group_count",
                    "schema_fingerprint_sha256",
                )
            }
            for index in selected_objects
        ],
    }


def validate_only(args: argparse.Namespace) -> dict[str, Any]:
    """P1-1: parse the inventory, validate the task shape against the frozen
    gates, and print the ranges this task WOULD read -- with no S3 access. Lets
    a dry-run honestly exercise the real Places projection argument + inventory
    schema path cheaply."""
    value = inventory.validate_inventory(json.loads(args.inventory.read_text()))
    inventory_file_sha256 = sha256_file(args.inventory)
    spec_sha256 = sha256_file(args.evidence_spec)
    if args.task_index < 0 or args.task_index >= len(value["map_plan"]["tasks"]):
        raise ValueError("Places projection task index is outside the inventory")
    task = value["map_plan"]["tasks"][args.task_index]
    selected_compressed = sum(item["selected_compressed_bytes"] for item in task["ranges"])
    if (
        task["expected_input_records"] > args.max_rows
        or task["row_groups"] > args.max_groups
        or selected_compressed > args.max_selected_compressed_bytes
        or task["selected_uncompressed_bytes"] > args.max_selected_uncompressed_bytes
    ):
        raise ValueError("Places projection task exceeds a frozen source gate")
    identity = projection_identity(value, task, inventory_file_sha256, spec_sha256)
    would_read = [
        {
            "uri": value["objects"][item["object_index"]]["uri"],
            "object_index": item["object_index"],
            "first_row_group": item["first_row_group"],
            "last_row_group": item["last_row_group"],
        }
        for item in task["ranges"]
    ]
    result = {
        "schema": "overture-places-construction-v1-validate-only-v1",
        "task_index": args.task_index,
        "inventory_sha256": identity["inventory_sha256"],
        "evidence_spec_sha256": spec_sha256,
        "expected_input_records": task["expected_input_records"],
        "row_groups": task["row_groups"],
        "selected_compressed_bytes": selected_compressed,
        "selected_uncompressed_bytes": task["selected_uncompressed_bytes"],
        "would_read_ranges": would_read,
        "s3_accessed": False,
    }
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return result


def run(args: argparse.Namespace, *, filesystem: Any | None = None) -> dict[str, Any]:
    import pyarrow as pa
    import pyarrow.fs as pafs
    import pyarrow.parquet as pq

    started = time.monotonic()
    value = inventory.validate_inventory(json.loads(args.inventory.read_text()))
    inventory_file_sha256 = sha256_file(args.inventory)
    spec_sha256 = sha256_file(args.evidence_spec)
    if args.task_index < 0 or args.task_index >= len(value["map_plan"]["tasks"]):
        raise ValueError("Places projection task index is outside the inventory")
    task = value["map_plan"]["tasks"][args.task_index]
    selected_compressed = sum(
        item["selected_compressed_bytes"] for item in task["ranges"]
    )
    if (
        task["expected_input_records"] > args.max_rows
        or task["row_groups"] > args.max_groups
        or selected_compressed > args.max_selected_compressed_bytes
        or task["selected_uncompressed_bytes"] > args.max_selected_uncompressed_bytes
    ):
        raise ValueError("Places projection task exceeds a frozen source gate")
    identity = projection_identity(value, task, inventory_file_sha256, spec_sha256)
    metadata = {
        b"overture.places_projection_identity": canonical_json(identity),
        b"overture.places_inventory_sha256": value["inventory_sha256"].encode(),
        b"overture.places_inventory_file_sha256": inventory_file_sha256.encode(),
        b"overture.places_schema_fingerprint_sha256": value["schema_contract"][
            "fingerprint_sha256"
        ].encode(),
        b"overture.places_evidence_spec_sha256": spec_sha256.encode(),
        b"overture.places_task_digest": task["task_digest"].encode(),
        b"overture.places_task_source_digest": task["source_digest"].encode(),
    }
    filesystem = filesystem or pafs.S3FileSystem(
        anonymous=True, region=inventory.REGION
    )
    writer = None
    bounded = BoundedWriter(args.output, args.max_output_bytes)
    rows = batches = maximum_batch_rows = 0
    checked: set[int] = set()
    # P0-1 integrity parity with the address projector: retain each object's
    # pre-read etag+byte identity so it can be re-verified AFTER the read.
    identities_before: dict[int, dict[str, Any]] = {}
    checked_uris: dict[int, str] = {}
    network_before = measured_network_received_bytes()
    try:
        for row_range in task["ranges"]:
            object_index = row_range["object_index"]
            source = value["objects"][object_index]
            if object_index not in checked:
                if args.skip_head_identity:
                    current = {"etag": source["etag"], "bytes": source["bytes"]}
                else:
                    current = head_identity(source["uri"])
                if current != {"etag": source["etag"], "bytes": source["bytes"]}:
                    raise ValueError("Places source identity changed after inventory")
                info = filesystem.get_file_info(source["uri"].removeprefix("s3://"))
                if info.size != source["bytes"]:
                    raise ValueError("Places source size changed after inventory")
                checked.add(object_index)
                identities_before[object_index] = current
                checked_uris[object_index] = source["uri"]
            parquet = pq.ParquetFile(
                source["uri"].removeprefix("s3://"), filesystem=filesystem
            )
            contract = inventory.schema_contract_from_arrow(parquet.schema_arrow)
            if contract["fingerprint_sha256"] != source["schema_fingerprint_sha256"]:
                raise ValueError("Places source schema changed after inventory")
            for group in range(
                row_range["first_row_group"], row_range["last_row_group"] + 1
            ):
                expected = source["row_groups"][group]["rows"]
                emitted = 0
                for batch in parquet.iter_batches(
                    batch_size=MAXIMUM_BATCH_ROWS,
                    row_groups=[group],
                    columns=sorted(inventory.PROJECTED_COLUMN_ROOTS),
                    use_threads=False,
                ):
                    flattened = flatten_batch(
                        batch,
                        object_index=object_index,
                        row_group=group,
                        row_offset=emitted,
                    )
                    if writer is None:
                        schema = flattened.schema.with_metadata(metadata)
                        writer = pq.ParquetWriter(
                            pa.PythonFile(bounded, mode="w"),
                            schema,
                            compression="zstd",
                            use_dictionary=True,
                            write_statistics=True,
                        )
                    writer.write_batch(flattened, row_group_size=MAXIMUM_BATCH_ROWS)
                    emitted += flattened.num_rows
                    rows += flattened.num_rows
                    batches += 1
                    maximum_batch_rows = max(maximum_batch_rows, flattened.num_rows)
                if emitted != expected:
                    raise ValueError(
                        "Places projected row group differs from inventory"
                    )
    finally:
        if writer is not None:
            writer.close()
        bounded.close()
    if writer is None or rows != task["expected_input_records"]:
        raise ValueError("Places projection row count differs from its task")
    output = pq.ParquetFile(args.output)
    if output.metadata.num_rows != rows or output.schema_arrow.metadata != metadata:
        raise ValueError("Places projection verification differs")
    # P0-1: re-verify every read object's etag+bytes AFTER the read, matching the
    # address projector's pre/post identity pin, so a mid-read source mutation is
    # caught before the artifact is trusted.
    if not args.skip_head_identity:
        for object_index, before in identities_before.items():
            after = head_identity(checked_uris[object_index])
            if after != before:
                raise ValueError("Places source identity changed during the read")
    network_after = measured_network_received_bytes()
    network_delta = (
        network_after - network_before
        if network_before is not None and network_after is not None
        else None
    )
    report = {
        "schema": SCHEMA,
        "identity": identity,
        "input": {
            "records": rows,
            "row_groups": task["row_groups"],
            "selected_compressed_bytes": selected_compressed,
            "selected_uncompressed_bytes": task["selected_uncompressed_bytes"],
        },
        "output": {
            "path": str(args.output),
            "bytes": args.output.stat().st_size,
            "sha256": sha256_file(args.output),
            "records": output.metadata.num_rows,
            "row_groups": output.metadata.num_row_groups,
        },
        "verification": {
            "pre_post_source_identity_match": not args.skip_head_identity,
            "output_record_count_match": True,
            "objects_identity_verified": sorted(identities_before),
        },
        "resources": {
            "elapsed_seconds": time.monotonic() - started,
            "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            * (1 if sys.platform == "darwin" else 1024),
            "remote_read_bytes": network_delta,
            "selected_compressed_bytes_planned": selected_compressed,
            "maximum_batch_rows": maximum_batch_rows,
            "batches": batches,
            "python_feature_rows_materialized": False,
        },
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--evidence-spec", type=Path, required=True)
    parser.add_argument("--task-index", type=int, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--validate-only", action="store_true",
                        help="Validate the task + print the ranges it would read; no S3, no output.")
    parser.add_argument("--max-rows", type=int, default=1_000_000)
    parser.add_argument("--max-groups", type=int, default=64)
    parser.add_argument(
        "--max-selected-compressed-bytes", type=int, default=536_870_912
    )
    parser.add_argument(
        "--max-selected-uncompressed-bytes", type=int, default=1_000_000_000
    )
    parser.add_argument("--max-output-bytes", type=int, default=536_870_912)
    parser.add_argument(
        "--skip-head-identity", action="store_true", help=argparse.SUPPRESS
    )
    args = parser.parse_args()
    if args.validate_only:
        print(json.dumps(validate_only(args), sort_keys=True))
        return
    if args.output is None or args.report is None:
        parser.error("--output and --report are required unless --validate-only")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    print(json.dumps(run(args), sort_keys=True))


if __name__ == "__main__":
    main()
