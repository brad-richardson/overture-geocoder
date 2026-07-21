#!/usr/bin/env python3
"""Execute and finalize bounded global-v2 address reduce jobs.

Each job scans its immutable map inputs once, routes records to stable partition
leaves, external-sorts with bounded buffers/open files, and invokes the existing
semantic artifact and Worker page producers.  Job IDs are scheduler identities,
never serving shard IDs.  Finalization proves the complete job/leaf matrix and
emits the Worker-readable address collection.
"""

from __future__ import annotations

import argparse
import bisect
import gzip
import heapq
import json
import os
import struct
import subprocess
import sys
import tempfile
import uuid
import zlib
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import global_v2_address_map as address_map  # noqa: E402
import global_v2_address_plan as address_plan  # noqa: E402
import experiment_address_compression as address_compression  # noqa: E402
from address_partition import (  # noqa: E402
    address_key_hash,
    normalize,
    validate_plan as validate_partition_plan,
)
from build_address_collection import build_collection_from_identities  # noqa: E402
from experiment_address_reduce import (  # noqa: E402
    FORMAT_VERSION,
    FRAGMENT_MAGIC,
    SPIKE_PARTITION_ID,
    FragmentReader,
    decode_record,
    encode_record,
    record_key,
    sha256_file,
    write_envelope,
)


JOB_COMPLETION_SCHEMA = "overture-global-v2-address-reduce-job-completion-v1"
REDUCE_COMPLETION_SCHEMA = "overture-global-v2-address-reduce-completion-v1"
MAX_OPEN_FILES = 64
MAX_SPILLS_PER_JOB = 1_000_000
DEFAULT_SORT_BUFFER_ROWS = 128_000
DEFAULT_SORT_BUFFER_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_WORKSPACE_BYTES = 12 * 1024 * 1024 * 1024
DEFAULT_MAX_ARTIFACT_BYTES = 4 * 1024 * 1024 * 1024
DEFAULT_MAX_SHARD_BYTES = 4 * 1024 * 1024 * 1024
WORKER_MAX_INDEX_BYTES = 4 * 1024 * 1024
WORKER_MAX_INDEX_ENTRIES = 65_536
WORKER_MAX_KEY_BYTES = 64 * 1024
WORKER_MAX_STORED_PAGE_BYTES = 256 * 1024
WORKER_MAX_DECODED_PAGE_BYTES = 1024 * 1024
WORKER_MAX_PAGE_ROWS = 10_000
WORKER_MAX_MATERIALIZED_BYTES = 8 * 1024 * 1024
WORKER_MAX_DICTIONARY_STRINGS = 100_000
WORKER_MAX_ADDRESS_LEVELS = 64
UINT32_MAX = (1 << 32) - 1
SELECTIVE_FETCH_PROOF_SCHEMA = "overture-r2-selective-parquet-v1"
SELECTIVE_FETCH_PROOF_VERSION = 1
SELECTIVE_FETCH_PLACEHOLDERS = (
    "{row_groups}",
    "{expected_bytes}",
    "{expected_sha256}",
    "{proof}",
)


class Workspace:
    def __init__(self, maximum: int):
        if type(maximum) is not int or maximum <= 0:
            raise ValueError("address reduce workspace cap must be positive")
        self.maximum = maximum
        self.current = 0
        self.peak = 0

    def add(self, size: int) -> None:
        if type(size) is not int or size < 0:
            raise ValueError("address reduce workspace accounting is invalid")
        self.current += size
        self.peak = max(self.peak, self.current)
        if self.current > self.maximum:
            raise ValueError("address reduce workspace exceeds its hard byte cap")

    def remove(self, size: int) -> None:
        self.current -= size
        if self.current < 0:
            raise ValueError("address reduce workspace accounting underflow")

    def remaining(self) -> int:
        return self.maximum - self.current


def _supports_selective_fetch(fetch_command: list[str] | None) -> bool:
    if fetch_command is None:
        return False
    counts = {
        placeholder: sum(item.count(placeholder) for item in fetch_command)
        for placeholder in SELECTIVE_FETCH_PLACEHOLDERS
    }
    if any(counts.values()) and set(counts.values()) != {1}:
        raise ValueError(
            "selective address fetch argv must contain each row-group/proof "
            "placeholder exactly once"
        )
    return all(value == 1 for value in counts.values())


def _whole_object_fetch_command(
    fetch_command: list[str] | None,
) -> list[str] | None:
    """Remove selective-only argv fields when fetching the complete pack."""

    if fetch_command is None or not _supports_selective_fetch(fetch_command):
        return fetch_command
    whole: list[str] = []
    for item in fetch_command:
        if any(
            placeholder in item for placeholder in SELECTIVE_FETCH_PLACEHOLDERS
        ):
            if whole and whole[-1].startswith("--") and "=" not in whole[-1]:
                whole.pop()
            continue
        whole.append(item)
    allowed = {"{object_key}", "{output}"}
    if any(
        ("{" in item or "}" in item) and item not in allowed for item in whole
    ):
        raise ValueError("selective fetch adapter has unsupported placeholders")
    return whole


def _expected_pack_header(
    raw_input: dict[str, Any], reduce: dict[str, Any]
) -> dict[str, Any]:
    identity = raw_input["address_task_identity"]
    return address_map.fragment_header(
        source_inventory_sha256=reduce["source"]["source_inventory_sha256"],
        schema_fingerprint_sha256=reduce["source"]["schema_fingerprint_sha256"],
        release=reduce["overture_release"],
        execution_bucket=identity["execution_bucket"],
        task_identity=identity,
        ownership=raw_input["intermediate_ownership"],
        index=raw_input["source_fragment_index"],
        records=raw_input["records"],
    )


def _validate_selective_fetch_proof(
    proof_path: Path,
    materialized_path: Path,
    *,
    raw_input: dict[str, Any],
    reduce: dict[str, Any],
    selected_row_groups: list[int],
) -> dict[str, Any]:
    proof = address_plan.load_json(proof_path)
    if proof_path.read_bytes() != address_plan.json_payload(proof):
        raise ValueError("selective address fetch proof is not canonical JSON")
    expected_header = _expected_pack_header(raw_input, reduce)
    selected_records = sum(
        raw_input["row_groups"][index]["records"]
        for index in selected_row_groups
    )
    expected_mapping = [
        {
            "materialized_index": materialized_index,
            "original_index": original_index,
            "records": raw_input["row_groups"][original_index]["records"],
            "compressed_column_bytes": raw_input["row_groups"][original_index][
                "compressed_column_bytes"
            ],
        }
        for materialized_index, original_index in enumerate(selected_row_groups)
    ]
    footer = proof.get("source_footer")
    materialized = proof.get("materialized")
    expected_footer = raw_input["parquet_layout_binding"]
    if (
        set(proof)
        != {
            "schema",
            "version",
            "object_key",
            "whole_object",
            "selected_original_row_groups",
            "source_header",
            "source_header_sha256",
            "source_footer",
            "materialized",
            "materialized_row_groups",
            "transport",
        }
        or proof.get("schema") != SELECTIVE_FETCH_PROOF_SCHEMA
        or proof.get("version") != SELECTIVE_FETCH_PROOF_VERSION
        or proof.get("object_key") != raw_input["object_key"]
        or proof.get("whole_object")
        != {
            "bytes": raw_input["bytes"],
            "sha256": raw_input["sha256"],
            "metadata_verified": True,
        }
        or proof.get("selected_original_row_groups") != selected_row_groups
        or proof.get("source_header") != expected_header
        or proof.get("source_header_sha256")
        != address_plan.value_sha256(expected_header)
        or not isinstance(footer, dict)
        or set(footer)
        != {
            "binding_sha256",
            "serialized_size",
            "records",
            "row_groups",
            "schema_sha256",
        }
        or type(footer.get("serialized_size")) is not int
        or footer
        != {
            "binding_sha256": expected_footer["sha256"],
            "serialized_size": expected_footer["serialized_size"],
            "records": expected_footer["records"],
            "row_groups": expected_footer["row_groups"],
            "schema_sha256": expected_footer["schema_sha256"],
        }
        or not isinstance(materialized, dict)
        or set(materialized) != {"bytes", "sha256", "row_groups", "records"}
        or type(materialized.get("bytes")) is not int
        or materialized["bytes"] <= 0
        or materialized.get("row_groups") != len(selected_row_groups)
        or materialized.get("records") != selected_records
        or proof.get("materialized_row_groups") != expected_mapping
        or proof.get("transport")
        != {
            "kind": "pyarrow-s3-random-access",
            "whole_object_downloaded": False,
            "whole_object_fallback_allowed": False,
        }
    ):
        raise ValueError("selective address fetch proof differs from the reduce plan")
    address_plan.require_sha256(
        footer["binding_sha256"], "selective source-footer binding"
    )
    address_plan.require_sha256(
        footer["schema_sha256"], "selective source schema binding"
    )
    address_plan.require_sha256(
        materialized["sha256"], "selective materialized SHA-256"
    )
    if (
        not materialized_path.is_file()
        or materialized_path.stat().st_size != materialized["bytes"]
        or sha256_file(materialized_path) != materialized["sha256"]
    ):
        raise ValueError("selective materialized Parquet identity differs")
    return proof


def _materialize_selected_remote_pack(
    raw_input: dict[str, Any],
    temporary_dir: Path,
    *,
    reduce: dict[str, Any],
    selected_row_groups: list[int],
    fetch_command: list[str] | None,
) -> tuple[Path, Path, dict[str, Any]]:
    if not _supports_selective_fetch(fetch_command):
        raise ValueError(
            "remote selective Parquet reduction requires a byte-range fetch "
            "adapter with row-group/footer support"
        )
    assert fetch_command is not None
    temporary_dir.mkdir(parents=True, exist_ok=True)
    output = temporary_dir / f"{raw_input['sha256']}.selected.parquet"
    proof_path = temporary_dir / f"{raw_input['sha256']}.selected.proof.json"
    if output.exists() or proof_path.exists():
        raise ValueError("selective address fetch targets unexpectedly exist")
    replacements = {
        "{object_key}": raw_input["object_key"],
        "{output}": str(output),
        "{row_groups}": json.dumps(selected_row_groups, separators=(",", ":")),
        "{expected_bytes}": str(raw_input["bytes"]),
        "{expected_sha256}": raw_input["sha256"],
        "{proof}": str(proof_path),
    }
    argv = []
    for item in fetch_command:
        for placeholder, value in replacements.items():
            item = item.replace(placeholder, value)
        argv.append(item)
    try:
        subprocess.run(argv, check=True)
        if not output.is_file() or not proof_path.is_file():
            raise ValueError("selective address fetch omitted output or proof")
        proof = _validate_selective_fetch_proof(
            proof_path,
            output,
            raw_input=raw_input,
            reduce=reduce,
            selected_row_groups=selected_row_groups,
        )
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        output.unlink(missing_ok=True)
        proof_path.unlink(missing_ok=True)
        if isinstance(exc, ValueError):
            raise
        raise ValueError("selective address fetch adapter failed") from exc
    return output, proof_path, proof


def _stage_immutable(source: Path, target: Path, *, digest: str, size: int) -> None:
    if (
        not source.is_file()
        or source.stat().st_size != size
        or sha256_file(source) != digest
    ):
        raise ValueError("address serving artifact differs before publication")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.stat().st_size != size or sha256_file(target) != digest:
            raise ValueError("existing immutable address serving artifact differs")
        return
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent, delete=False
        ) as output:
            temporary = Path(output.name)
        temporary.unlink()
        os.link(source, temporary)
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _plan_identity(path: Path, expected: dict[str, Any]) -> None:
    if (
        not path.is_file()
        or path.stat().st_size != expected.get("bytes")
        or sha256_file(path) != expected.get("sha256")
    ):
        raise ValueError("address partition plan identity differs from reduce plan")


def load_plans(
    partition_path: Path, reduce_path: Path
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    partition = address_plan.load_json(partition_path)
    leaves = validate_partition_plan(partition)
    lineage = address_plan.partition_lineage(partition)
    reduce = address_plan.load_json(reduce_path)
    _plan_identity(partition_path, reduce.get("partition_plan", {}))
    serving = reduce.get("serving_configuration")
    if (
        reduce.get("schema") != address_plan.REDUCE_PLAN_SCHEMA
        or reduce.get("overture_release") != partition.get("overture_release")
        or reduce.get("normalization_version") != partition.get("normalization_version")
        or reduce.get("source") != partition.get("source")
        or reduce.get("partition_lineage") != lineage
        or reduce.get("serving_runtime_contract")
        != address_plan.serving_runtime_contract()
        or not isinstance(serving, dict)
        or serving.get("variant") != "useful_gzip"
        or type(serving.get("page_rows")) is not int
        or not 1 <= serving["page_rows"] <= 4_096
        or type(serving.get("sparse_stride")) is not int
        or serving["sparse_stride"] <= 0
        or type(serving.get("max_page_rows")) is not int
        or serving["max_page_rows"] < serving["page_rows"]
        or serving["max_page_rows"] > WORKER_MAX_PAGE_ROWS
        or not isinstance(reduce.get("inputs"), list)
        or not isinstance(reduce.get("jobs"), list)
        or not 1 <= len(reduce["jobs"]) <= address_plan.MAX_REDUCE_JOBS
        or len(leaves) > address_plan.MAX_SERVING_ROUTES
    ):
        raise ValueError("address reduce plan is incompatible or runtime-unpinned")
    inputs = reduce["inputs"]
    if len(inputs) > address_plan.MAX_SOURCE_FRAGMENTS:
        raise ValueError("address reduce input count exceeds its hard cap")
    seen_digests: set[str] = set()
    for index, item in enumerate(inputs):
        ownership = (
            item.get("intermediate_ownership") if isinstance(item, dict) else None
        )
        if (
            not isinstance(item, dict)
            or item.get("index") != index
            or item.get("format") != address_map.WIRE_ENCODING
            or item.get("sha256") in seen_digests
            or type(item.get("bytes")) is not int
            or item["bytes"] <= 0
            or type(item.get("records")) is not int
            or item["records"] <= 0
            or not isinstance(item.get("object_key"), str)
            or not item["object_key"]
            or not isinstance(item.get("address_task_identity"), dict)
            or not isinstance(item.get("row_groups"), list)
            or not item["row_groups"]
            or not isinstance(ownership, dict)
            or ownership
            != address_map.intermediate_ownership(
                ownership.get("country"),
                ownership.get("minimum_bucket"),
                ownership.get("maximum_bucket"),
                partition["partition"]["maximum_hash_bits"],
            )
        ):
            raise ValueError("address reduce input identity is invalid or replayed")
        address_plan.require_sha256(item["sha256"], "address reduce input sha256")
        address_map.validate_parquet_layout_binding(
            item.get("parquet_layout_binding"),
            expected_records=item["records"],
            expected_row_groups=len(item["row_groups"]),
        )
        if sum(group.get("records", -1) for group in item["row_groups"]) != item[
            "records"
        ]:
            raise ValueError("address reduce row-group records differ")
        previous_row_group_end: int | None = None
        for row_group_index, row_group in enumerate(item["row_groups"]):
            group_ownership = (
                row_group.get("intermediate_ownership")
                if isinstance(row_group, dict)
                else None
            )
            if (
                not isinstance(row_group, dict)
                or row_group.get("index") != row_group_index
                or type(row_group.get("records")) is not int
                or row_group["records"] <= 0
                or type(row_group.get("compressed_column_bytes")) is not int
                or row_group["compressed_column_bytes"] <= 0
                or not isinstance(group_ownership, dict)
                or group_ownership
                != address_map.intermediate_ownership(
                    group_ownership.get("country"),
                    group_ownership.get("minimum_bucket"),
                    group_ownership.get("maximum_bucket"),
                    partition["partition"]["maximum_hash_bits"],
                )
                or group_ownership["country"] != ownership["country"]
                or group_ownership["minimum_bucket"]
                < ownership["minimum_bucket"]
                or group_ownership["maximum_bucket"]
                > ownership["maximum_bucket"]
                or (
                    previous_row_group_end is not None
                    and group_ownership["minimum_bucket"]
                    < previous_row_group_end
                )
                or row_group.get("integrity")
                != {
                    "kind": "canonical-row-multiset-binding-v1",
                    "order_verified_by_consumer": True,
                }
            ):
                raise ValueError("address reduce row-group proof is invalid")
            address_plan.validate_semantic_binding(
                row_group.get("semantic_binding"),
                expected_records=row_group["records"],
            )
            previous_row_group_end = group_ownership["maximum_bucket"]
        if "relative_path" in item:
            address_plan.safe_relative_path(item["relative_path"], "reduce input path")
        seen_digests.add(item["sha256"])

    nonempty = {leaf["id"]: leaf for leaf in leaves if leaf["rows"] > 0}
    for leaf in leaves:
        address_plan.validate_semantic_binding(
            leaf.get("semantic_binding"), expected_records=leaf["rows"]
        )
    seen_partitions: set[str] = set()
    for index, job in enumerate(reduce["jobs"]):
        if (
            not isinstance(job, dict)
            or job.get("index") != index
            or job.get("id") != f"address-reduce-job-{index:03d}"
            or job.get("kind") != address_plan.REDUCE_JOB_KIND
            or job.get("is_serving_shard_id") is not False
            or not isinstance(job.get("partition_ids"), list)
            or not job["partition_ids"]
            or len(set(job["partition_ids"])) != len(job["partition_ids"])
            or any(identifier not in nonempty for identifier in job["partition_ids"])
            or seen_partitions.intersection(job["partition_ids"])
            or not isinstance(job.get("input_indexes"), list)
            or len(set(job["input_indexes"])) != len(job["input_indexes"])
            or any(
                type(item) is not int or not 0 <= item < len(inputs)
                for item in job["input_indexes"]
            )
            or not isinstance(job.get("input_row_groups"), list)
            or job.get("expected_rows")
            != sum(nonempty[identifier]["rows"] for identifier in job["partition_ids"])
            or job.get("expected_semantic_binding")
            != address_plan.combine_semantic_bindings(
                [
                    nonempty[identifier]["semantic_binding"]
                    for identifier in job["partition_ids"]
                ],
                expected_records=job.get("expected_rows"),
            )
        ):
            raise ValueError("address reduce job matrix is invalid or duplicated")
        expected_inputs = [
            {
                "input_index": item["index"],
                "row_group_indexes": [
                    row_group["index"]
                    for row_group in item["row_groups"]
                    if any(
                        row_group["intermediate_ownership"]["country"]
                        == nonempty[identifier]["country"]
                        and row_group["intermediate_ownership"]["hash_start"]
                        <= nonempty[identifier]["hash_end"]
                        and row_group["intermediate_ownership"]["hash_end"]
                        >= nonempty[identifier]["hash_start"]
                        for identifier in job["partition_ids"]
                    )
                ],
            }
            for item in inputs
        ]
        expected_inputs = [item for item in expected_inputs if item["row_group_indexes"]]
        if (
            job["input_row_groups"] != expected_inputs
            or job["input_indexes"]
            != [item["input_index"] for item in expected_inputs]
        ):
            raise ValueError("address reduce job input fan-in is not exact")
        seen_partitions.update(job["partition_ids"])
    totals = reduce.get("totals")
    input_references = [
        (assignment["input_index"], row_group_index)
        for job in reduce["jobs"]
        for assignment in job["input_row_groups"]
        for row_group_index in assignment["row_group_indexes"]
    ]
    input_reference_counts = Counter(input_references)
    if (
        seen_partitions != set(nonempty)
        or not isinstance(totals, dict)
        or totals.get("inputs") != len(inputs)
        or totals.get("input_bytes") != sum(item["bytes"] for item in inputs)
        or totals.get("input_records") != sum(item["records"] for item in inputs)
        or totals.get("jobs") != len(reduce["jobs"])
        or totals.get("nonempty_partitions") != len(nonempty)
        or totals.get("expected_rows")
        != sum(leaf["rows"] for leaf in nonempty.values())
        or totals.get("input_references") != len(input_references)
        or totals.get("input_reference_bytes")
        != sum(
            inputs[index]["row_groups"][row_group_index]["compressed_column_bytes"]
            for index, row_group_index in input_references
        )
        or totals.get("maximum_job_inputs")
        != max(
            sum(
                len(assignment["row_group_indexes"])
                for assignment in job["input_row_groups"]
            )
            for job in reduce["jobs"]
        )
        or totals.get("maximum_fragment_job_references")
        != max(input_reference_counts.values(), default=0)
        or totals.get("maximum_fragment_job_references")
        > address_plan.MAX_FRAGMENT_JOB_REFERENCES
        or totals["expected_rows"] != partition["totals"]["retained_rows"]
    ):
        raise ValueError("address reduce plan totals or leaf coverage differ")
    return partition, reduce, leaves


def _serving_sort_identity(
    key: tuple[str, ...], payload: bytes, maximum_hash_bits: int
) -> tuple[tuple[str, ...], int, int, int]:
    """Order one stable hash leaf by the Worker's lookup-key contract."""

    total = address_map.record_total_sort_identity(key, payload, maximum_hash_bits)
    return total[2], total[3], total[4], total[5]


def _write_spill(
    path: Path,
    records: list[tuple[tuple[str, ...], bytes]],
    *,
    source_inventory_sha256: str,
    fragment_index: int,
    maximum_hash_bits: int,
) -> dict[str, Any]:
    records.sort(
        key=lambda item: _serving_sort_identity(
            item[0], item[1], maximum_hash_bits
        )
    )
    with path.open("wb") as output:
        write_envelope(
            output,
            FRAGMENT_MAGIC,
            {
                "format": FORMAT_VERSION,
                "records": len(records),
                "source_inventory_sha256": source_inventory_sha256,
                "fragment_index": fragment_index,
                "partition_id": SPIKE_PARTITION_ID,
            },
        )
        for _, payload in records:
            output.write(struct.pack("<I", len(payload)))
            output.write(payload)
    return {
        "index": fragment_index,
        "partition_id": SPIKE_PARTITION_ID,
        "path": str(path),
        "bytes": path.stat().st_size,
        "records": len(records),
        "sha256": sha256_file(path),
    }


def _merge_spills(
    spills: list[dict[str, Any]],
    output_path: Path,
    *,
    source_inventory_sha256: str,
    fragment_index: int,
    maximum_hash_bits: int,
) -> dict[str, Any]:
    readers = [
        FragmentReader(
            Path(item["path"]),
            ordering_key=lambda key, payload: _serving_sort_identity(
                key, payload, maximum_hash_bits
            ),
        )
        for item in spills
    ]
    expected_rows = sum(item["records"] for item in spills)
    temporary: Path | None = None
    try:
        heap: list[tuple[tuple[tuple[str, ...], int, int, int], int, bytes]] = []
        for index, (manifest, reader) in enumerate(zip(spills, readers)):
            if (
                reader.header.get("records") != manifest["records"]
                or reader.header.get("source_inventory_sha256")
                != source_inventory_sha256
                or reader.header.get("partition_id") != SPIKE_PARTITION_ID
                or Path(manifest["path"]).stat().st_size != manifest["bytes"]
                or sha256_file(Path(manifest["path"])) != manifest["sha256"]
            ):
                raise ValueError("address sort spill differs before compaction")
            item = reader.next()
            if item is not None:
                heapq.heappush(
                    heap,
                    (
                        _serving_sort_identity(
                            item[0], item[1], maximum_hash_bits
                        ),
                        index,
                        item[1],
                    ),
                )
        with tempfile.NamedTemporaryFile(
            prefix=".address-spill-merge-",
            suffix=".tmp",
            dir=output_path.parent,
            delete=False,
        ) as output:
            temporary = Path(output.name)
            write_envelope(
                output,
                FRAGMENT_MAGIC,
                {
                    "format": FORMAT_VERSION,
                    "records": expected_rows,
                    "source_inventory_sha256": source_inventory_sha256,
                    "fragment_index": fragment_index,
                    "partition_id": SPIKE_PARTITION_ID,
                },
            )
            rows = 0
            previous: tuple[tuple[str, ...], int, int, int] | None = None
            while heap:
                identity, reader_index, payload = heapq.heappop(heap)
                if previous is not None and identity < previous:
                    raise ValueError("address compacted spill is not sorted")
                output.write(struct.pack("<I", len(payload)))
                output.write(payload)
                rows += 1
                previous = identity
                item = readers[reader_index].next()
                if item is not None:
                    heapq.heappush(
                        heap,
                        (
                            _serving_sort_identity(
                                item[0], item[1], maximum_hash_bits
                            ),
                            reader_index,
                            item[1],
                        ),
                    )
            if rows != expected_rows:
                raise ValueError("address compacted spill rows do not reconcile")
        os.replace(temporary, output_path)
        temporary = None
    finally:
        for reader in readers:
            reader.close()
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {
        "index": fragment_index,
        "partition_id": SPIKE_PARTITION_ID,
        "path": str(output_path),
        "bytes": output_path.stat().st_size,
        "records": expected_rows,
        "sha256": sha256_file(output_path),
    }


def _compact_to_one(
    spills: list[dict[str, Any]],
    temp_dir: Path,
    *,
    source_inventory_sha256: str,
    max_open_files: int,
    workspace: Workspace,
    name_prefix: str,
    maximum_hash_bits: int,
) -> dict[str, Any]:
    if not spills:
        raise ValueError("non-empty address leaf has no sort spills")
    level = 0
    current = spills
    merge_fan_in = max_open_files - 3
    while len(current) > merge_fan_in:
        next_level = []
        for group_index, offset in enumerate(range(0, len(current), merge_fan_in)):
            group = current[offset : offset + merge_fan_in]
            estimated = sum(item["bytes"] for item in group) + 4096
            if estimated > workspace.remaining():
                raise ValueError("address spill compaction exceeds workspace cap")
            output = temp_dir / f"{name_prefix}-l{level + 1}-{group_index:06d}.bin"
            merged = _merge_spills(
                group,
                output,
                source_inventory_sha256=source_inventory_sha256,
                fragment_index=group_index,
                maximum_hash_bits=maximum_hash_bits,
            )
            workspace.add(merged["bytes"])
            for item in group:
                Path(item["path"]).unlink()
                workspace.remove(item["bytes"])
            next_level.append(merged)
        current = next_level
        level += 1
    if len(current) == 1 and current[0]["index"] == 0:
        return current[0]
    estimated = sum(item["bytes"] for item in current) + 4096
    if estimated > workspace.remaining():
        raise ValueError("address final spill compaction exceeds workspace cap")
    output = temp_dir / f"{name_prefix}-final.bin"
    merged = _merge_spills(
        current,
        output,
        source_inventory_sha256=source_inventory_sha256,
        fragment_index=0,
        maximum_hash_bits=maximum_hash_bits,
    )
    workspace.add(merged["bytes"])
    for item in current:
        Path(item["path"]).unlink()
        workspace.remove(item["bytes"])
    return merged


def _build_serving_from_sorted_spill(
    spill: dict[str, Any],
    output_dir: Path,
    *,
    leaf: dict[str, Any],
    page_rows: int,
    max_page_rows: int,
    max_output_bytes: int,
    max_workspace_bytes: int,
    maximum_hash_bits: int,
) -> dict[str, Any]:
    """Write Worker pages directly from the single externally sorted run.

    This deliberately removes the `.ared` semantic envelope. The input spill
    is already the bounded external-sort result; this pass revalidates its
    order and ownership, recomputes the semantic binding, and writes the final
    page/index pair once.
    """

    path = Path(spill["path"])
    if (
        path.stat().st_size != spill["bytes"]
        or sha256_file(path) != spill["sha256"]
        or spill["records"] != leaf["rows"]
    ):
        raise ValueError("address final sorted spill identity differs")
    output_dir.mkdir(parents=True, exist_ok=True)
    index_path = output_dir / f"{leaf['id']}.aidx"
    data_path = output_dir / f"{leaf['id']}.adat"
    if index_path.exists() or data_path.exists():
        raise ValueError("address direct serving outputs are create-only")

    data_header = address_plan.canonical_json(
        {"format": 2, "variant": "useful_gzip", "page_rows": page_rows}
    )
    reader = FragmentReader(
        path,
        ordering_key=lambda key, payload: _serving_sort_identity(
            key, payload, maximum_hash_bits
        ),
    )
    if reader.header.get("records") != leaf["rows"]:
        reader.close()
        raise ValueError("address final spill rows differ from stable leaf")
    rows = pages = distinct_keys = maximum_fanout = 0
    previous_identity: tuple[tuple[str, ...], int, int, int] | None = None
    current_prefix: tuple[str, ...] | None = None
    current_count = 0
    semantic = address_plan.SemanticAccumulator()
    pending: tuple[tuple[str, ...], bytes] | None = None

    def finish_group() -> None:
        nonlocal distinct_keys, maximum_fanout
        if current_prefix is not None:
            distinct_keys += 1
            maximum_fanout = max(maximum_fanout, current_count)

    temporary_index: Path | None = None
    temporary_data: Path | None = None
    try:
        with (
            tempfile.NamedTemporaryFile(
                prefix=f".{index_path.name}.",
                suffix=".tmp",
                dir=output_dir,
                delete=False,
            ) as index_file,
            tempfile.NamedTemporaryFile(
                prefix=f".{data_path.name}.",
                suffix=".tmp",
                dir=output_dir,
                delete=False,
            ) as data_file,
        ):
            temporary_index = Path(index_file.name)
            temporary_data = Path(data_file.name)
            index_file.write(address_compression.INDEX_MAGIC)
            data_file.write(
                address_compression.DATA_MAGIC
                + struct.pack("<I", len(data_header))
                + data_header
            )
            exhausted = False
            while not exhausted or pending is not None:
                page: list[dict[str, Any]] = []
                if pending is not None:
                    key, payload = pending
                    page.append(decode_record(payload))
                    pending = None
                while not exhausted:
                    item = reader.next()
                    if item is None:
                        exhausted = True
                        break
                    key, payload = item
                    prefix = key[:8]
                    if page and len(page) >= page_rows and prefix != page[-1]["key"][:8]:
                        pending = item
                        break
                    page.append(decode_record(payload))
                    if len(page) > max_page_rows:
                        raise ValueError(
                            "address candidate group exceeds the Worker page-row cap"
                        )
                if not page:
                    continue
                for record in page:
                    key = record["key"]
                    prefix = key[:8]
                    payload = encode_record(record)
                    identity = _serving_sort_identity(
                        key, payload, maximum_hash_bits
                    )
                    if previous_identity is not None and identity < previous_identity:
                        raise ValueError("address direct serving input is not sorted")
                    if key[0] != leaf["country"]:
                        raise ValueError("address direct serving input crosses country")
                    hashed = address_key_hash(prefix)
                    if not leaf["hash_start"] <= hashed <= leaf["hash_end"]:
                        raise ValueError("address direct serving input crosses leaf")
                    if prefix != current_prefix:
                        finish_group()
                        current_prefix = prefix
                        current_count = 0
                    current_count += 1
                    semantic.add(address_plan.canonical_semantic_payload(payload, key))
                    previous_identity = identity
                    rows += 1
                raw = address_compression.encode_page(page, useful=True)
                if len(raw) > WORKER_MAX_DECODED_PAGE_BYTES:
                    raise ValueError("address decoded page exceeds the Worker cap")
                stored = gzip.compress(raw, compresslevel=6, mtime=0)
                if len(stored) > WORKER_MAX_STORED_PAGE_BYTES:
                    raise ValueError("address stored page exceeds the Worker cap")
                offset = data_file.tell()
                data_file.write(struct.pack("<I", len(stored)))
                data_file.write(stored)
                first_key = b"".join(
                    address_compression.encode_text(value)
                    for value in page[0]["key"][:8]
                )
                if len(first_key) > WORKER_MAX_KEY_BYTES:
                    raise ValueError("address serving index key exceeds the Worker cap")
                index_file.write(address_compression.encode_uvarint(offset))
                index_file.write(address_compression.encode_uvarint(4 + len(stored)))
                index_file.write(address_compression.encode_uvarint(len(page)))
                index_file.write(address_compression.encode_uvarint(len(first_key)))
                index_file.write(first_key)
                pages += 1
                if (
                    index_file.tell() > WORKER_MAX_INDEX_BYTES
                    or index_file.tell() > max_output_bytes
                    or data_file.tell() > max_output_bytes
                    or spill["bytes"] + index_file.tell() + data_file.tell()
                    > max_workspace_bytes
                ):
                    raise ValueError("address direct serving output exceeds a hard cap")
            finish_group()
        reader.close()
        if rows != leaf["rows"]:
            raise ValueError("address direct serving rows do not reconcile")
        os.replace(temporary_index, index_path)
        temporary_index = None
        os.replace(temporary_data, data_path)
        temporary_data = None
    finally:
        reader.close()
        if temporary_index is not None:
            temporary_index.unlink(missing_ok=True)
        if temporary_data is not None:
            temporary_data.unlink(missing_ok=True)

    return {
        "rows": rows,
        "pages": pages,
        "distinct_lookup_keys": distinct_keys,
        "maximum_candidate_fanout": maximum_fanout,
        "semantic_binding": semantic.finish(),
        "artifacts": {
            "index": {
                "path": str(index_path),
                "bytes": index_path.stat().st_size,
                "sha256": sha256_file(index_path),
            },
            "data": {
                "path": str(data_path),
                "bytes": data_path.stat().st_size,
                "sha256": sha256_file(data_path),
            },
        },
    }


def _input_path(item: dict[str, Any], input_root: Path) -> dict[str, Any]:
    resolved = dict(item)
    relative = item.get("relative_path")
    if relative is not None:
        resolved["source_path"] = input_root / address_plan.safe_relative_path(
            relative, "address reduce input path"
        )
    return resolved


def run_job(
    partition_path: Path,
    reduce_path: Path,
    *,
    job_id: str,
    input_root: Path,
    output_root: Path,
    fragment_fetch_command: list[str] | None = None,
    sort_buffer_rows: int = DEFAULT_SORT_BUFFER_ROWS,
    sort_buffer_bytes: int = DEFAULT_SORT_BUFFER_BYTES,
    max_open_files: int = MAX_OPEN_FILES,
    max_workspace_bytes: int = DEFAULT_MAX_WORKSPACE_BYTES,
    max_artifact_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
    max_shard_bytes: int = DEFAULT_MAX_SHARD_BYTES,
) -> dict[str, Any]:
    partition, reduce, leaves = load_plans(partition_path, reduce_path)
    matches = [job for job in reduce["jobs"] if job["id"] == job_id]
    if len(matches) != 1:
        raise ValueError("address reduce job ID is absent or duplicated")
    if (
        min(sort_buffer_rows, sort_buffer_bytes, max_artifact_bytes, max_shard_bytes)
        <= 0
        or not 5 <= max_open_files <= MAX_OPEN_FILES
    ):
        raise ValueError("address reduce limits are outside hard bounds")
    job = matches[0]
    leaf_by_id = {leaf["id"]: leaf for leaf in leaves}
    job_leaves = [leaf_by_id[identifier] for identifier in job["partition_ids"]]
    routes: dict[str, tuple[list[int], list[dict[str, Any]]]] = {}
    for country in sorted({leaf["country"] for leaf in job_leaves}):
        country_leaves = sorted(
            [leaf for leaf in job_leaves if leaf["country"] == country],
            key=lambda leaf: leaf["hash_start"],
        )
        routes[country] = (
            [leaf["hash_start"] for leaf in country_leaves],
            country_leaves,
        )

    output_root.mkdir(parents=True, exist_ok=True)
    workspace = Workspace(max_workspace_bytes)
    selected_rows: defaultdict[str, int] = defaultdict(int)
    semantic_accumulators = {
        leaf["id"]: address_plan.SemanticAccumulator() for leaf in job_leaves
    }
    buffers: defaultdict[str, list[tuple[tuple[str, ...], bytes]]] = defaultdict(list)
    spills: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    buffered_rows = buffered_bytes = 0
    spill_sequence = 0
    source_inventory_sha256 = reduce["source"]["source_inventory_sha256"]
    maximum_hash_bits = partition["partition"]["maximum_hash_bits"]
    serving = reduce["serving_configuration"]
    selected_row_group_count = sum(
        len(item["row_group_indexes"]) for item in job["input_row_groups"]
    )
    available_row_group_count = sum(
        len(reduce["inputs"][item["input_index"]]["row_groups"])
        for item in job["input_row_groups"]
    )
    selected_compressed_column_bytes = sum(
        reduce["inputs"][item["input_index"]]["row_groups"][row_group_index][
            "compressed_column_bytes"
        ]
        for item in job["input_row_groups"]
        for row_group_index in item["row_group_indexes"]
    )
    remote_whole_pack_fetches = 0
    remote_selective_pack_fetches = 0

    with tempfile.TemporaryDirectory(prefix=f".{job_id}-", dir=output_root) as name:
        temp = Path(name)
        fetch_slot = temp / "fetch"

        def flush_buffers() -> None:
            nonlocal buffered_rows, buffered_bytes, spill_sequence
            for identifier in sorted(buffers):
                records = buffers[identifier]
                if not records:
                    continue
                if sum(len(items) for items in spills.values()) >= MAX_SPILLS_PER_JOB:
                    raise ValueError("address sort spill count exceeds its hard cap")
                path = temp / f"spill-{identifier}-{spill_sequence:08d}.bin"
                estimated = sum(4 + len(payload) for _, payload in records) + 4096
                if estimated > workspace.remaining():
                    raise ValueError("address sort spill exceeds workspace cap")
                manifest = _write_spill(
                    path,
                    records,
                    source_inventory_sha256=source_inventory_sha256,
                    fragment_index=len(spills[identifier]),
                    maximum_hash_bits=maximum_hash_bits,
                )
                workspace.add(manifest["bytes"])
                spills[identifier].append(manifest)
                spill_sequence += 1
            buffers.clear()
            buffered_rows = buffered_bytes = 0

        for input_assignment in job["input_row_groups"]:
            input_index = input_assignment["input_index"]
            selected_row_groups = input_assignment["row_group_indexes"]
            raw_input = _input_path(reduce["inputs"][input_index], input_root)
            source = raw_input.get("source_path")
            is_remote = not isinstance(source, Path) or not source.is_file()
            selects_whole_pack = selected_row_groups == list(
                range(len(raw_input["row_groups"]))
            )
            selective_remote = (
                is_remote
                and not selects_whole_pack
                and _supports_selective_fetch(fragment_fetch_command)
            )
            if (
                is_remote
                and not selective_remote
                and selected_row_groups
                != list(range(len(raw_input["row_groups"])))
            ):
                raise ValueError(
                    "remote selective Parquet reduction requires a byte-range "
                    "fetch adapter with row-group/footer support"
                )
            if (
                is_remote
                and not selective_remote
                and raw_input["bytes"] > workspace.remaining()
            ):
                raise ValueError(
                    "address remote fragment exceeds the remaining workspace cap"
                )
            proof_path: Path | None = None
            if selective_remote:
                path, proof_path, proof = _materialize_selected_remote_pack(
                    raw_input,
                    fetch_slot,
                    reduce=reduce,
                    selected_row_groups=selected_row_groups,
                    fetch_command=fragment_fetch_command,
                )
                remote_selective_pack_fetches += 1
                materialized_groups = [
                    (item["materialized_index"], item["original_index"])
                    for item in proof["materialized_row_groups"]
                ]
                remove_after = True
            else:
                path, remove_after = address_plan.materialized_fragment(
                    raw_input,
                    fetch_slot,
                    fetch_command=_whole_object_fetch_command(
                        fragment_fetch_command
                    ),
                )
                materialized_groups = [
                    (row_group_index, row_group_index)
                    for row_group_index in selected_row_groups
                ]
                if is_remote:
                    remote_whole_pack_fetches += 1
            fetched_bytes = 0
            try:
                if remove_after:
                    fetched_bytes = path.stat().st_size
                    if fetched_bytes > workspace.remaining():
                        raise ValueError(
                            "address fetched fragment exceeds the remaining workspace cap"
                        )
                    workspace.add(fetched_bytes)
                if not selective_remote and (
                    path.stat().st_size != raw_input["bytes"]
                    or sha256_file(path) != raw_input["sha256"]
                ):
                    raise ValueError("address reduce input content identity differs")
                if not selective_remote:
                    import pyarrow.parquet as pq

                    if (
                        address_map.parquet_layout_binding(pq.ParquetFile(path))
                        != raw_input["parquet_layout_binding"]
                    ):
                        raise ValueError(
                            "address reduce input footer/layout binding differs"
                        )
                expected_header = _expected_pack_header(raw_input, reduce)
                if selective_remote:
                    expected_header = {
                        **expected_header,
                        "records": sum(
                            raw_input["row_groups"][index]["records"]
                            for index in selected_row_groups
                        ),
                    }
                previous_pack_identity = None
                for materialized_index, original_index in materialized_groups:
                    row_group = raw_input["row_groups"][original_index]
                    reader = address_map.CountryFragmentReader(
                        path,
                        maximum_hash_bits=maximum_hash_bits,
                        row_groups=[materialized_index],
                    )
                    if reader.header != expected_header:
                        raise ValueError(
                            "address reduce input header differs from plan"
                        )
                    try:
                        while True:
                            record = reader.next()
                            if record is None:
                                break
                            key, payload = record
                            pack_identity = address_map.record_total_sort_identity(
                                key, payload, maximum_hash_bits
                            )
                            if (
                                previous_pack_identity is not None
                                and pack_identity < previous_pack_identity
                            ):
                                raise ValueError(
                                    "address selected row groups are not totally sorted"
                                )
                            previous_pack_identity = pack_identity
                            country = key[0]
                            route = routes.get(country)
                            if route is None:
                                continue
                            hashed = address_key_hash(key[:8])
                            starts, country_leaves = route
                            route_index = bisect.bisect_right(starts, hashed) - 1
                            if route_index < 0:
                                continue
                            leaf = country_leaves[route_index]
                            if hashed > leaf["hash_end"]:
                                continue
                            record_bytes = (
                                4
                                + len(payload)
                                + sum(len(value.encode()) for value in key)
                                + 256
                            )
                            if record_bytes > sort_buffer_bytes:
                                raise ValueError(
                                    "one address record exceeds the sort memory cap"
                                )
                            if buffered_rows and (
                                buffered_rows >= sort_buffer_rows
                                or buffered_bytes + record_bytes > sort_buffer_bytes
                            ):
                                flush_buffers()
                            buffers[leaf["id"]].append((key, payload))
                            buffered_rows += 1
                            buffered_bytes += record_bytes
                            selected_rows[leaf["id"]] += 1
                            semantic_accumulators[leaf["id"]].add(
                                address_plan.canonical_semantic_payload(payload, key)
                            )
                    finally:
                        reader.close()
                    if reader.semantic_binding() != row_group["semantic_binding"]:
                        raise ValueError(
                            "address row-group semantic integrity proof differs"
                        )
            finally:
                if remove_after:
                    path.unlink(missing_ok=True)
                    if proof_path is not None:
                        proof_path.unlink(missing_ok=True)
                    if fetched_bytes:
                        workspace.remove(fetched_bytes)
        flush_buffers()
        if any(selected_rows[leaf["id"]] != leaf["rows"] for leaf in job_leaves):
            raise ValueError("address records do not map exactly once to job leaves")
        if sum(selected_rows.values()) != job["expected_rows"]:
            raise ValueError("address reduce job retained rows do not reconcile")
        semantic_bindings = {
            leaf["id"]: semantic_accumulators[leaf["id"]].finish()
            for leaf in job_leaves
        }
        for leaf in job_leaves:
            if semantic_bindings[leaf["id"]] != leaf["semantic_binding"]:
                raise ValueError(
                    "address reducer semantic content differs from its plan binding"
                )

        artifacts = []
        reduce_plan_sha256 = sha256_file(reduce_path)
        partition_plan_sha256 = sha256_file(partition_path)
        for leaf in job_leaves:
            identifier = leaf["id"]
            final_spill = _compact_to_one(
                spills[identifier],
                temp,
                source_inventory_sha256=source_inventory_sha256,
                max_open_files=max_open_files,
                workspace=workspace,
                name_prefix=identifier,
                maximum_hash_bits=maximum_hash_bits,
            )
            shard_temp = temp / f"serving-{identifier}"
            shard = _build_serving_from_sorted_spill(
                final_spill,
                shard_temp,
                leaf=leaf,
                page_rows=serving["page_rows"],
                max_page_rows=serving["max_page_rows"],
                max_output_bytes=max_shard_bytes,
                max_workspace_bytes=workspace.remaining() + final_spill["bytes"],
                maximum_hash_bits=maximum_hash_bits,
            )
            index_source = Path(shard["artifacts"]["index"]["path"])
            data_source = Path(shard["artifacts"]["data"]["path"])
            index_identity = shard["artifacts"]["index"]
            data_identity = shard["artifacts"]["data"]
            emitted = _validate_serving_artifacts(
                index_source,
                data_source,
                leaf=leaf,
                serving=serving,
                maximum_hash_bits=maximum_hash_bits,
            )
            if (
                shard["semantic_binding"] != semantic_bindings[identifier]
                or emitted["rows"] != shard["rows"]
                or emitted["distinct_lookup_keys"] != shard["distinct_lookup_keys"]
                or emitted["maximum_candidate_fanout"]
                != shard["maximum_candidate_fanout"]
                or emitted["semantic_binding"] != semantic_bindings[identifier]
            ):
                raise ValueError(
                    "emitted address serving artifact differs from its semantic input"
                )
            index_target = output_root / f"families/addresses/shards/{identifier}.aidx"
            data_target = output_root / f"families/addresses/shards/{identifier}.adat"
            _stage_immutable(
                index_source,
                index_target,
                digest=index_identity["sha256"],
                size=index_identity["bytes"],
            )
            _stage_immutable(
                data_source,
                data_target,
                digest=data_identity["sha256"],
                size=data_identity["bytes"],
            )
            artifacts.append(
                {
                    "partition_id": identifier,
                    "rows": shard["rows"],
                    "distinct_lookup_keys": shard["distinct_lookup_keys"],
                    "maximum_candidate_fanout": shard["maximum_candidate_fanout"],
                    "semantic_binding": semantic_bindings[identifier],
                    "index": {
                        "relative_path": index_target.relative_to(
                            output_root
                        ).as_posix(),
                        "bytes": index_identity["bytes"],
                        "sha256": index_identity["sha256"],
                    },
                    "data": {
                        "relative_path": data_target.relative_to(
                            output_root
                        ).as_posix(),
                        "bytes": data_identity["bytes"],
                        "sha256": data_identity["sha256"],
                    },
                }
            )
            Path(final_spill["path"]).unlink()
            workspace.remove(final_spill["bytes"])

        completion = {
            "schema": JOB_COMPLETION_SCHEMA,
            "construction": {
                "kind": "direct-serving-from-external-sort-v1",
                "semantic_intermediate": None,
                "row_group_routing": {
                    "selected": selected_row_group_count,
                    "available_in_referenced_packs": available_row_group_count,
                    "selected_compressed_column_bytes": (
                        selected_compressed_column_bytes
                    ),
                    "remote_whole_pack_fetches": remote_whole_pack_fetches,
                    "remote_selective_pack_fetches": remote_selective_pack_fetches,
                    "partial_read_integrity": (
                        "canonical-row-multiset-binding-v1"
                    ),
                },
            },
            "overture_release": partition["overture_release"],
            "partition_lineage": reduce["partition_lineage"],
            "job": job,
            "partition_plan_sha256": partition_plan_sha256,
            "reduce_plan_sha256": reduce_plan_sha256,
            "serving_runtime_contract": reduce["serving_runtime_contract"],
            "artifacts": artifacts,
            "accounting": {
                "expected_rows": job["expected_rows"],
                "output_rows": sum(item["rows"] for item in artifacts),
                "partitions": len(artifacts),
                "distinct_lookup_keys": sum(
                    item["distinct_lookup_keys"] for item in artifacts
                ),
                "maximum_candidate_fanout": max(
                    item["maximum_candidate_fanout"] for item in artifacts
                ),
                "peak_temporary_workspace_bytes": workspace.peak,
                "semantic_binding": address_plan.combine_semantic_bindings(
                    [semantic_bindings[leaf["id"]] for leaf in job_leaves],
                    expected_records=job["expected_rows"],
                ),
            },
        }
        if completion["accounting"]["output_rows"] != job["expected_rows"]:
            raise ValueError("address job outputs do not reconcile")
        completion_path = (
            output_root / f"families/addresses/reduce-completions/{job_id}.json"
        )
        address_plan.write_create_or_verify(completion_path, completion)
        return completion


def _read_uvarint(payload: bytes, position: int) -> tuple[int, int]:
    return address_compression.decode_uvarint(payload, position)


def _read_bounded_text(payload: bytes, position: int) -> tuple[str, int]:
    length, position = _read_uvarint(payload, position)
    end = position + length
    if length > WORKER_MAX_KEY_BYTES or end > len(payload):
        raise ValueError("address serving text is outside Worker hard bounds")
    try:
        return payload[position:end].decode("utf-8"), end
    except UnicodeDecodeError as exc:
        raise ValueError("address serving text is not UTF-8") from exc


def _parse_serving_index(path: Path) -> list[dict[str, Any]]:
    if path.stat().st_size > WORKER_MAX_INDEX_BYTES:
        raise ValueError("address serving index exceeds the Worker hard byte cap")
    payload = path.read_bytes()
    if not payload.startswith(address_compression.INDEX_MAGIC):
        raise ValueError("invalid address serving index magic")
    position = len(address_compression.INDEX_MAGIC)
    previous_key: tuple[str, ...] | None = None
    previous_end = 0
    entries = []
    while position < len(payload):
        if len(entries) >= WORKER_MAX_INDEX_ENTRIES:
            raise ValueError("address serving index entry cap exceeded")
        offset, position = _read_uvarint(payload, position)
        length, position = _read_uvarint(payload, position)
        rows, position = _read_uvarint(payload, position)
        key_length, position = _read_uvarint(payload, position)
        key_end = position + key_length
        if key_length > WORKER_MAX_KEY_BYTES or key_end > len(payload):
            raise ValueError("address serving index key is outside Worker hard bounds")
        key_position = position
        key = []
        for _ in range(8):
            value, key_position = _read_bounded_text(payload, key_position)
            key.append(value)
        if key_position != key_end:
            raise ValueError("address serving index key framing is invalid")
        normalized = tuple(key)
        end = offset + length
        if (
            rows <= 0
            or rows > WORKER_MAX_PAGE_ROWS
            or length <= 4
            or length > WORKER_MAX_STORED_PAGE_BYTES + 4
            or offset < previous_end
            or (previous_key is not None and normalized <= previous_key)
        ):
            raise ValueError("address serving index extent or ordering is invalid")
        entries.append(
            {"key": normalized, "offset": offset, "length": length, "rows": rows}
        )
        previous_key = normalized
        previous_end = end
        position = key_end
    if not entries:
        raise ValueError("address serving index is empty")
    return entries


def _decode_serving_page(payload: bytes, expected_rows: int) -> list[dict[str, Any]]:
    position = 0
    rows, position = _read_uvarint(payload, position)
    if rows != expected_rows or not 1 <= rows <= WORKER_MAX_PAGE_ROWS:
        raise ValueError("decoded address row count differs from its index")
    string_count, position = _read_uvarint(payload, position)
    if string_count > WORKER_MAX_DICTIONARY_STRINGS:
        raise ValueError("address serving dictionary entry cap exceeded")
    strings = []
    for _ in range(string_count):
        value, position = _read_bounded_text(payload, position)
        strings.append(value)
    sequence_count, position = _read_uvarint(payload, position)
    if sequence_count > rows:
        raise ValueError("address serving sequence count exceeds its row count")
    sequences = []
    for _ in range(sequence_count):
        count, position = _read_uvarint(payload, position)
        if count > WORKER_MAX_ADDRESS_LEVELS:
            raise ValueError("address level count exceeds the Worker hard cap")
        sequence = []
        for _ in range(count):
            value, position = _read_uvarint(payload, position)
            if value >= len(strings):
                raise ValueError("address level dictionary ID is out of range")
            sequence.append(value)
        sequences.append(sequence)

    previous_fields = (b"",) * 8
    previous_full: tuple[tuple[str, ...], bytes, int, int, int] | None = None
    materialized_bytes = 0
    records = []
    for _ in range(rows):
        fields = []
        encoded_fields = []
        for old in previous_fields:
            prefix, position = _read_uvarint(payload, position)
            suffix_length, position = _read_uvarint(payload, position)
            suffix_end = position + suffix_length
            if (
                prefix > len(old)
                or suffix_length > WORKER_MAX_KEY_BYTES
                or suffix_end > len(payload)
            ):
                raise ValueError(
                    "front-coded address key is outside Worker hard bounds"
                )
            encoded = old[:prefix] + payload[position:suffix_end]
            try:
                fields.append(encoded.decode("utf-8"))
            except UnicodeDecodeError as exc:
                raise ValueError("front-coded address key is not UTF-8") from exc
            encoded_fields.append(encoded)
            position = suffix_end
        previous_fields = tuple(encoded_fields)
        end = position + 24
        if end > len(payload):
            raise ValueError("truncated address serving record core")
        identifier = payload[position : position + 16]
        longitude, latitude = struct.unpack_from("<ii", payload, position + 16)
        position = end
        if not (
            -1_800_000_000 <= longitude <= 1_800_000_000
            and -900_000_000 <= latitude <= 900_000_000
        ):
            raise ValueError("address serving coordinates are outside valid bounds")
        source_locators = []
        for _ in range(3):
            value, position = _read_uvarint(payload, position)
            if value > UINT32_MAX:
                raise ValueError("address serving source locator exceeds u32")
            source_locators.append(value)
        display = []
        for _ in range(6):
            value, position = _read_uvarint(payload, position)
            if value >= len(strings):
                raise ValueError("address display dictionary ID is out of range")
            display.append(value)
        sequence_id, position = _read_uvarint(payload, position)
        if sequence_id >= len(sequences):
            raise ValueError("address level sequence ID is out of range")
        key = tuple(fields)
        full = (key, identifier, *source_locators)
        if previous_full is not None and full < previous_full:
            raise ValueError("address serving page records are not sorted")
        previous_full = full
        string_bytes = sum(len(value) for value in encoded_fields) + sum(
            len(strings[index].encode()) for index in display
        )
        levels = sequences[sequence_id]
        string_bytes += sum(len(strings[index].encode()) for index in levels)
        materialized_bytes += string_bytes + 256 + len(levels) * 32
        if materialized_bytes > WORKER_MAX_MATERIALIZED_BYTES:
            raise ValueError("materialized address page exceeds the Worker heap budget")
        record = {
            "key": key + (str(uuid.UUID(bytes=identifier)),),
            "id": str(uuid.UUID(bytes=identifier)),
            "lon": longitude / 10_000_000,
            "lat": latitude / 10_000_000,
            "source_object_index": source_locators[0],
            "source_row_group": source_locators[1],
            "source_row_index": source_locators[2],
            "country": strings[display[0]],
            "postal_city": strings[display[1]],
            "postcode": strings[display[2]],
            "street": strings[display[3]],
            "number": strings[display[4]],
            "unit": strings[display[5]],
            "address_levels": [strings[index] for index in levels],
        }
        if record_key(record) != record["key"]:
            raise ValueError(
                "address serving display fields differ from their normalized key"
            )
        records.append(
            {
                "key": key,
                "identifier": identifier,
                "source_locator": tuple(source_locators),
                "semantic_payload": encode_record(record),
            }
        )
    if position != len(payload):
        raise ValueError("address serving page has trailing decoded bytes")
    return records


def _validate_serving_artifacts(
    index_path: Path,
    data_path: Path,
    *,
    leaf: dict[str, Any],
    serving: dict[str, Any],
    maximum_hash_bits: int,
) -> dict[str, Any]:
    entries = _parse_serving_index(index_path)
    data_size = data_path.stat().st_size
    with data_path.open("rb") as source:
        prefix = source.read(len(address_compression.DATA_MAGIC) + 4)
        if len(prefix) != len(
            address_compression.DATA_MAGIC
        ) + 4 or not prefix.startswith(address_compression.DATA_MAGIC):
            raise ValueError("invalid address serving data magic")
        header_length = struct.unpack_from(
            "<I", prefix, len(address_compression.DATA_MAGIC)
        )[0]
        if header_length > WORKER_MAX_KEY_BYTES:
            raise ValueError("address serving data header exceeds Worker hard bounds")
        header_payload = source.read(header_length)
        if len(header_payload) != header_length:
            raise ValueError("truncated address serving data header")
        try:
            header = json.loads(header_payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid address serving data header JSON") from exc
        if (
            not isinstance(header, dict)
            or header.get("format") != 2
            or header.get("variant") != "useful_gzip"
            or type(header.get("page_rows")) is not int
            or header.get("page_rows") != serving["page_rows"]
            or set(header) != {"format", "variant", "page_rows"}
        ):
            raise ValueError("unsupported address serving data header")
        expected_offset = source.tell()
        total_rows = distinct_keys = maximum_fanout = 0
        previous_full: tuple[tuple[str, ...], int, int, int] | None = None
        previous_lookup_key: tuple[str, ...] | None = None
        candidate_count = 0
        semantic_accumulator = address_plan.SemanticAccumulator()
        smoke_sample: dict[str, Any] | None = None
        for entry in entries:
            if entry["offset"] != expected_offset:
                raise ValueError("address serving page directory is not contiguous")
            source.seek(entry["offset"])
            frame = source.read(entry["length"])
            if len(frame) != entry["length"]:
                raise ValueError("truncated address serving page")
            stored_length = struct.unpack_from("<I", frame)[0]
            if stored_length != len(frame) - 4:
                raise ValueError("address serving page length differs from its index")
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
            try:
                decoded = decoder.decompress(
                    frame[4:], WORKER_MAX_DECODED_PAGE_BYTES + 1
                )
            except zlib.error as exc:
                raise ValueError("invalid gzip address serving page") from exc
            if (
                len(decoded) > WORKER_MAX_DECODED_PAGE_BYTES
                or decoder.unconsumed_tail
                or decoder.unused_data
                or not decoder.eof
            ):
                raise ValueError("decoded address serving page exceeds hard bounds")
            decoded += decoder.flush()
            if len(decoded) > WORKER_MAX_DECODED_PAGE_BYTES:
                raise ValueError("decoded address serving page exceeds hard bounds")
            records = _decode_serving_page(decoded, entry["rows"])
            if records[0]["key"] != entry["key"]:
                raise ValueError("address serving index key differs from its page")
            if (
                previous_lookup_key is not None
                and records[0]["key"] == previous_lookup_key
            ):
                raise ValueError("address candidate group crosses serving pages")
            for record in records:
                key = record["key"]
                semantic_record = decode_record(record["semantic_payload"])
                full = _serving_sort_identity(
                    semantic_record["key"],
                    record["semantic_payload"],
                    maximum_hash_bits,
                )
                if previous_full is not None and full < previous_full:
                    raise ValueError("address serving records are not globally sorted")
                if (
                    len(key) != 8
                    or any(normalize(value) != value for value in key)
                    or key[0] != leaf["country"]
                    or not leaf["hash_start"]
                    <= address_key_hash(key)
                    <= leaf["hash_end"]
                ):
                    raise ValueError(
                        "address serving record crosses its stable partition"
                    )
                if key != previous_lookup_key:
                    if previous_lookup_key is not None:
                        distinct_keys += 1
                        maximum_fanout = max(maximum_fanout, candidate_count)
                    previous_lookup_key = key
                    candidate_count = 0
                candidate_count += 1
                total_rows += 1
                semantic_accumulator.add(record["semantic_payload"])
                if smoke_sample is None:
                    key = record["key"]
                    smoke_sample = {
                        "country": key[0],
                        "admin_level_general": key[1],
                        "admin_level_specific": key[2],
                        "postal_city": key[3],
                        "postcode": key[4],
                        "street": key[5],
                        "number": key[6],
                        "unit": key[7],
                        "expected_id": str(uuid.UUID(bytes=record["identifier"])),
                        "source": "verified-serving-page-first-record",
                    }
                previous_full = full
            expected_offset = entry["offset"] + entry["length"]
        if expected_offset != data_size:
            raise ValueError("address serving data has unindexed trailing bytes")
    if previous_lookup_key is not None:
        distinct_keys += 1
        maximum_fanout = max(maximum_fanout, candidate_count)
    if total_rows != leaf["rows"]:
        raise ValueError("address serving artifact rows differ from its stable leaf")
    return {
        "rows": total_rows,
        "pages": len(entries),
        "distinct_lookup_keys": distinct_keys,
        "maximum_candidate_fanout": maximum_fanout,
        "semantic_binding": semantic_accumulator.finish(),
        "smoke_sample": smoke_sample,
    }


def _materialize_serving_object(
    *,
    local_path: Path,
    object_key: str,
    identity: dict[str, Any],
    slot: Path,
    fetch_command: list[str] | None,
    materializer: Callable[[str, Path], None] | None,
) -> tuple[Path, bool]:
    if local_path.is_file():
        return local_path, False
    if fetch_command is None and materializer is None:
        raise ValueError(
            "address serving artifact is not local and has no fetch adapter"
        )
    target = slot / Path(object_key).name
    if target.exists():
        raise ValueError("address serving fetch target unexpectedly exists")
    try:
        if materializer is not None:
            materializer(object_key, target)
        else:
            assert fetch_command is not None
            argv = [
                item.replace("{object_key}", object_key).replace(
                    "{output}", str(target)
                )
                for item in fetch_command
            ]
            subprocess.run(argv, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        target.unlink(missing_ok=True)
        raise ValueError("address serving artifact fetch adapter failed") from exc
    if (
        not target.is_file()
        or target.stat().st_size != identity["bytes"]
        or sha256_file(target) != identity["sha256"]
    ):
        raise ValueError("fetched address serving artifact content identity differs")
    return target, True


def finalize(
    partition_path: Path,
    reduce_path: Path,
    completion_paths: Iterable[Path],
    *,
    output_root: Path,
    artifact_fetch_command: list[str] | None = None,
    artifact_materializer: Callable[[str, Path], None] | None = None,
) -> dict[str, Any]:
    if artifact_fetch_command is not None and artifact_materializer is not None:
        raise ValueError("address finalizer accepts only one artifact fetch adapter")
    if artifact_fetch_command is not None:
        validated_fetch = address_plan.parse_fetch_command(
            json.dumps(artifact_fetch_command)
        )
        if validated_fetch != artifact_fetch_command:
            raise ValueError("address artifact fetch command is not canonical")
    partition, reduce, leaves = load_plans(partition_path, reduce_path)
    supplied = list(completion_paths)
    if len(supplied) != len(reduce["jobs"]) or len(
        set(path.resolve() for path in supplied)
    ) != len(supplied):
        raise ValueError("address reduce completion matrix is missing or duplicated")
    expected_jobs = {job["id"]: job for job in reduce["jobs"]}
    by_job: dict[str, dict[str, Any]] = {}
    artifact_identities: dict[str, dict[str, Any]] = {}
    artifact_reports: dict[str, dict[str, Any]] = {}
    smoke_samples: list[dict[str, Any]] = []
    fetched_objects = fetched_bytes = 0
    peak_staged_files = peak_staged_bytes = 0
    partition_sha256 = sha256_file(partition_path)
    reduce_sha256 = sha256_file(reduce_path)
    for path in supplied:
        completion = address_plan.load_json(path)
        job = completion.get("job")
        job_id = job.get("id") if isinstance(job, dict) else None
        if (
            job_id not in expected_jobs
            or job_id in by_job
            or job != expected_jobs[job_id]
            or completion.get("schema") != JOB_COMPLETION_SCHEMA
            or completion.get("overture_release") != partition["overture_release"]
            or completion.get("partition_lineage")
            != reduce["partition_lineage"]
            or completion.get("partition_plan_sha256") != partition_sha256
            or completion.get("reduce_plan_sha256") != reduce_sha256
            or completion.get("serving_runtime_contract")
            != reduce["serving_runtime_contract"]
            or not isinstance(completion.get("artifacts"), list)
        ):
            raise ValueError(
                "address reduce completion identity is invalid or replayed"
            )
        expected_partitions = set(job["partition_ids"])
        seen: set[str] = set()
        for artifact in completion["artifacts"]:
            identifier = (
                artifact.get("partition_id") if isinstance(artifact, dict) else None
            )
            if identifier not in expected_partitions or identifier in seen:
                raise ValueError(
                    "address reduce artifact leaf is missing or duplicated"
                )
            leaf = next(item for item in leaves if item["id"] == identifier)
            index = artifact.get("index")
            data = artifact.get("data")
            if (
                artifact.get("rows") != leaf["rows"]
                or type(artifact.get("distinct_lookup_keys")) is not int
                or not 1 <= artifact["distinct_lookup_keys"] <= artifact["rows"]
                or type(artifact.get("maximum_candidate_fanout")) is not int
                or not 1 <= artifact["maximum_candidate_fanout"] <= artifact["rows"]
                or not isinstance(artifact.get("semantic_binding"), dict)
                or not isinstance(index, dict)
                or not isinstance(data, dict)
                or type(index.get("bytes")) is not int
                or index["bytes"] <= 0
                or type(data.get("bytes")) is not int
                or data["bytes"] <= 0
            ):
                raise ValueError("address reduce artifact accounting is invalid")
            address_plan.require_sha256(index.get("sha256"), "address index sha256")
            address_plan.require_sha256(data.get("sha256"), "address data sha256")
            index_path = output_root / address_plan.safe_relative_path(
                index.get("relative_path"), "address shard index path"
            )
            data_path = output_root / address_plan.safe_relative_path(
                data.get("relative_path"), "address shard data path"
            )
            if index["relative_path"] != (
                f"families/addresses/shards/{identifier}.aidx"
            ) or data["relative_path"] != (
                f"families/addresses/shards/{identifier}.adat"
            ):
                raise ValueError("address serving artifact content identity differs")
            output_root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(
                prefix=f".address-finalize-{identifier}-", dir=output_root
            ) as slot_name:
                slot = Path(slot_name)
                materialized_index, fetched_index = _materialize_serving_object(
                    local_path=index_path,
                    object_key=index["relative_path"],
                    identity=index,
                    slot=slot,
                    fetch_command=artifact_fetch_command,
                    materializer=artifact_materializer,
                )
                materialized_data, fetched_data = _materialize_serving_object(
                    local_path=data_path,
                    object_key=data["relative_path"],
                    identity=data,
                    slot=slot,
                    fetch_command=artifact_fetch_command,
                    materializer=artifact_materializer,
                )
                fetched = [
                    (materialized_index, index) if fetched_index else None,
                    (materialized_data, data) if fetched_data else None,
                ]
                fetched = [item for item in fetched if item is not None]
                staged_paths = {path for path, _ in fetched}
                if (
                    len(fetched) > 2
                    or any(not path.is_file() for path, _ in fetched)
                    or set(slot.iterdir()) != staged_paths
                ):
                    raise ValueError("address finalizer staging cardinality is invalid")
                staged_bytes = sum(identity["bytes"] for _, identity in fetched)
                peak_staged_files = max(peak_staged_files, len(fetched))
                peak_staged_bytes = max(peak_staged_bytes, staged_bytes)
                fetched_objects += len(fetched)
                fetched_bytes += staged_bytes
                for path, identity in (
                    (materialized_index, index),
                    (materialized_data, data),
                ):
                    if (
                        path.stat().st_size != identity["bytes"]
                        or sha256_file(path) != identity["sha256"]
                    ):
                        raise ValueError(
                            "address serving artifact content identity differs"
                        )
                verification = _validate_serving_artifacts(
                    materialized_index,
                    materialized_data,
                    leaf=leaf,
                    serving=reduce["serving_configuration"],
                    maximum_hash_bits=partition["partition"][
                        "maximum_hash_bits"
                    ],
                )
            if verification["semantic_binding"] != leaf["semantic_binding"]:
                raise ValueError(
                    "address serving semantic content differs from its plan binding"
                )
            if (
                verification["rows"] != artifact["rows"]
                or verification["distinct_lookup_keys"]
                != artifact["distinct_lookup_keys"]
                or verification["maximum_candidate_fanout"]
                != artifact["maximum_candidate_fanout"]
                or verification["semantic_binding"] != artifact["semantic_binding"]
            ):
                raise ValueError(
                    "address serving artifact contents differ from completion accounting"
                )
            if not isinstance(verification["smoke_sample"], dict):
                raise ValueError("address serving artifact has no deterministic smoke sample")
            smoke_samples.append(verification["smoke_sample"])
            artifact_identities[identifier] = {
                "index_bytes": index["bytes"],
                "index_sha256": index["sha256"],
                "data_bytes": data["bytes"],
                "data_sha256": data["sha256"],
            }
            artifact_reports[identifier] = artifact
            seen.add(identifier)
        accounting = completion.get("accounting")
        if (
            seen != expected_partitions
            or not isinstance(accounting, dict)
            or accounting.get("expected_rows") != job["expected_rows"]
            or accounting.get("output_rows") != job["expected_rows"]
            or accounting.get("partitions") != len(seen)
            or accounting.get("distinct_lookup_keys")
            != sum(item["distinct_lookup_keys"] for item in completion["artifacts"])
            or accounting.get("maximum_candidate_fanout")
            != max(item["maximum_candidate_fanout"] for item in completion["artifacts"])
            or accounting.get("semantic_binding")
            != expected_jobs[job_id]["expected_semantic_binding"]
        ):
            raise ValueError("address reduce completion accounting does not reconcile")
        by_job[job_id] = completion
    expected_leaves = {leaf["id"] for leaf in leaves if leaf["rows"] > 0}
    if set(by_job) != set(expected_jobs) or set(artifact_identities) != expected_leaves:
        raise ValueError("address final shards do not cover stable leaves exactly once")
    if not smoke_samples:
        raise ValueError("address finalization has no serving smoke sample")
    output_rows = sum(item["rows"] for item in artifact_reports.values())
    maximum_fanout = max(
        item["maximum_candidate_fanout"] for item in artifact_reports.values()
    )
    lower_bound = partition["accounting"]["exact_lookup_fanout"][
        "task_maximum_lower_bound"
    ]
    if (
        output_rows != partition["totals"]["retained_rows"]
        or maximum_fanout < lower_bound
    ):
        raise ValueError("address global reduce proof differs from map accounting")
    materialization = {
        "kind": "one-serving-pair-at-a-time-v1",
        "fetched_objects": fetched_objects,
        "fetched_bytes": fetched_bytes,
        "maximum_simultaneous_files": 2,
        "peak_simultaneous_staged_files": peak_staged_files,
        "peak_staged_bytes": peak_staged_bytes,
        "exact_content_identity_verified": True,
    }
    collection = build_collection_from_identities(partition, artifact_identities)
    collection.update(
        {
            "source": partition["source"],
            "partition_lineage": reduce["partition_lineage"],
            "partition_plan_sha256": partition_sha256,
            "reduce_plan_sha256": reduce_sha256,
            "serving_runtime_contract": reduce["serving_runtime_contract"],
            "exact_lookup_fanout": {
                "scope": "global",
                "maximum_candidates": maximum_fanout,
                "distinct_lookup_keys": sum(
                    item["distinct_lookup_keys"] for item in artifact_reports.values()
                ),
            },
            "artifact_materialization": materialization,
        }
    )
    collection_path = output_root / "families/addresses/address-collection.json"
    collection_identity = address_plan.write_create_or_verify(
        collection_path, collection
    )
    collection_identity["relative_path"] = collection_path.relative_to(
        output_root
    ).as_posix()
    completion = {
        "schema": REDUCE_COMPLETION_SCHEMA,
        "overture_release": partition["overture_release"],
        "partition_lineage": reduce["partition_lineage"],
        "partition_plan_sha256": partition_sha256,
        "reduce_plan_sha256": reduce_sha256,
        "serving_runtime_contract": reduce["serving_runtime_contract"],
        "jobs": {
            "expected": len(expected_jobs),
            "completed": len(by_job),
            "completion_set_sha256": address_plan.value_sha256(
                [by_job[job_id] for job_id in sorted(by_job)]
            ),
        },
        "accounting": {
            "retained_rows": partition["totals"]["retained_rows"],
            "final_shard_rows": output_rows,
            "serving_shards": len(artifact_identities),
            "leaf_assignments_exactly_once": True,
            "final_shards_exactly_once": True,
            "exact_lookup_fanout": collection["exact_lookup_fanout"],
        },
        "artifact_materialization": materialization,
        "collection": collection_identity,
        "smoke_sample": min(
            smoke_samples,
            key=lambda item: (
                item["country"], item["street"], item["number"], item["expected_id"]
            ),
        ),
    }
    completion_path = output_root / "families/addresses/reduce-completion.json"
    address_plan.write_create_or_verify(completion_path, completion)
    return completion


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--partition-plan", type=Path, required=True)
    common.add_argument("--reduce-plan", type=Path, required=True)
    common.add_argument("--output-root", type=Path, required=True)

    job = subparsers.add_parser("run-job", parents=[common])
    job.add_argument("--job-id", required=True)
    job.add_argument("--input-root", type=Path, required=True)
    job.add_argument("--fragment-fetch-command-json")
    job.add_argument("--sort-buffer-rows", type=int, default=DEFAULT_SORT_BUFFER_ROWS)
    job.add_argument("--sort-buffer-bytes", type=int, default=DEFAULT_SORT_BUFFER_BYTES)
    job.add_argument("--max-open-files", type=int, default=MAX_OPEN_FILES)
    job.add_argument(
        "--max-workspace-bytes", type=int, default=DEFAULT_MAX_WORKSPACE_BYTES
    )
    job.add_argument(
        "--max-artifact-bytes", type=int, default=DEFAULT_MAX_ARTIFACT_BYTES
    )
    job.add_argument("--max-shard-bytes", type=int, default=DEFAULT_MAX_SHARD_BYTES)

    final = subparsers.add_parser("finalize", parents=[common])
    final.add_argument(
        "--completion", type=Path, action="append", default=[], required=True
    )
    final.add_argument("--artifact-fetch-command-json")
    args = parser.parse_args()
    if args.command == "run-job":
        result = run_job(
            args.partition_plan,
            args.reduce_plan,
            job_id=args.job_id,
            input_root=args.input_root,
            output_root=args.output_root,
            fragment_fetch_command=address_plan.parse_fetch_command(
                args.fragment_fetch_command_json
            ),
            sort_buffer_rows=args.sort_buffer_rows,
            sort_buffer_bytes=args.sort_buffer_bytes,
            max_open_files=args.max_open_files,
            max_workspace_bytes=args.max_workspace_bytes,
            max_artifact_bytes=args.max_artifact_bytes,
            max_shard_bytes=args.max_shard_bytes,
        )
    else:
        result = finalize(
            args.partition_plan,
            args.reduce_plan,
            args.completion,
            output_root=args.output_root,
            artifact_fetch_command=address_plan.parse_fetch_command(
                args.artifact_fetch_command_json
            ),
        )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
