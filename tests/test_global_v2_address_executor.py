from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import sqlite3
import struct
import sys
import uuid
from pathlib import Path

import pytest


pa = pytest.importorskip("pyarrow")
pafs = pytest.importorskip("pyarrow.fs")
pq = pytest.importorskip("pyarrow.parquet")

SCRIPT_DIR = Path(__file__).parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import global_v2_address_map as address_map  # noqa: E402
import global_v2_address_plan as address_plan  # noqa: E402
import global_v2_address_reduce as address_reduce  # noqa: E402
import inventory_address_rowgroups as inventory  # noqa: E402
import experiment_address_compression as address_compression  # noqa: E402
from address_partition import address_key_hash  # noqa: E402
from experiment_address_compression import indexed_lookup  # noqa: E402


RELEASE = "2026-06-17.0"


def predecessor_family_manifest(
    *, generation: int = 1, digest: str = "9" * 64
) -> dict:
    return {
        "object_key": (
            f"slice-2026-07-18.{generation}/families/addresses/family-manifest.json"
        ),
        "bytes": 1_234,
        "sha256": digest,
    }


def point(lon: float = -71.0, lat: float = 42.0) -> bytes:
    return b"\x01" + struct.pack("<Idd", 1, lon, lat)


def required_table(row_count: int):
    level_type = pa.list_(pa.struct([pa.field("value", pa.string())]))
    return pa.table(
        {
            "id": [str(uuid.UUID(int=index + 1)) for index in range(row_count)],
            "street": ["Main Street"] * row_count,
            "number": [str(index + 1) for index in range(row_count)],
            "unit": [""] * row_count,
            "postcode": ["02180"] * row_count,
            "postal_city": ["Stoneham"] * row_count,
            "address_levels": pa.array(
                [[{"value": "MA"}]] * row_count, type=level_type
            ),
            "country": ["US"] * row_count,
            "geometry": pa.array([point()] * row_count, type=pa.binary()),
        }
    )


def canonical_inventory(tmp_path: Path) -> dict:
    source_path = tmp_path / "source.parquet"
    pq.write_table(required_table(4), source_path, row_group_size=2)
    source = inventory.inventory_object(
        {
            "uri": f"s3://{source_path}",
            "etag": "source-etag",
            "bytes": source_path.stat().st_size,
        },
        pafs.LocalFileSystem(),
    )
    source["uri"] = (
        f"s3://{inventory.BUCKET}/release/{RELEASE}/"
        "theme=addresses/type=address/part-00000.parquet"
    )
    plan = inventory.plan_contiguous_ranges(
        [source],
        target_rows=2,
        max_selected_uncompressed_bytes=10_000_000,
        max_groups=1,
        max_tasks=8,
    )
    report = inventory.build_report(RELEASE, [source], plan)
    assert len(report["plan"]["tasks"]) == 2
    inventory.validate_canonical_inventory(report)
    return report


def projected_row(feature_id: int, number: str, row_group: int, row_index: int):
    return {
        "id": str(uuid.UUID(int=feature_id)),
        "street": "Main Street",
        "number": number,
        "unit": "",
        "postcode": "02180",
        "postal_city": "Stoneham",
        "address_levels": [{"value": "MA"}, {"value": "Stoneham"}],
        "country": "US",
        "geometry": point(),
        "source_object_index": 0,
        "source_row_group": row_group,
        "source_row_index": row_index,
    }


def write_projected(path: Path, report: dict, task: dict, rows: list[dict]) -> None:
    source_inventory = report["source_inventory"]
    source_json = json.dumps(
        source_inventory, sort_keys=True, separators=(",", ":")
    ).encode()
    table = pa.Table.from_pylist(rows).replace_schema_metadata(
        {
            b"overture.source_inventory_sha256": hashlib.sha256(source_json)
            .hexdigest()
            .encode(),
            b"overture.source_inventory_json": source_json,
            b"overture.release": RELEASE.encode(),
            b"overture.family": b"addresses",
            address_map.SCHEMA_FINGERPRINT_METADATA_KEY: report["schema_contract"][
                "fingerprint_sha256"
            ].encode(),
            inventory.INVENTORY_METADATA_KEY: report["inventory_sha256"].encode(),
            inventory.TASK_INDEX_METADATA_KEY: str(task["index"]).encode(),
            inventory.TASK_DIGEST_METADATA_KEY: task["task_digest_sha256"].encode(),
            inventory.TASK_SOURCE_DIGEST_METADATA_KEY: task[
                "source_digest_sha256"
            ].encode(),
            inventory.EXECUTION_BUCKET_METADATA_KEY: task["execution_bucket"].encode(),
        }
    )
    pq.write_table(table, path)


def map_matrix(tmp_path: Path, report: dict, *, maximum_hash_bits: int = 4):
    ten_key = (
        "us",
        "ma",
        "stoneham",
        "stoneham",
        "02180",
        "main street",
        "10",
        "",
    )
    ten_bucket = address_map.hash_bucket(
        address_map.record_hash(ten_key), maximum_hash_bits
    )
    other_number = next(
        str(value)
        for value in range(11, 1000)
        if address_map.hash_bucket(
            address_map.record_hash((*ten_key[:6], str(value), "")),
            maximum_hash_bits,
        )
        != ten_bucket
    )
    rows = [
        [projected_row(101, "10", 0, 0), projected_row(102, "10", 0, 1)],
        [
            projected_row(103, "10", 1, 0),
            projected_row(104, other_number, 1, 1),
        ],
    ]
    result = []
    for task, task_rows in zip(report["plan"]["tasks"], rows):
        root = tmp_path / f"map-{task['index']}"
        root.mkdir()
        projected = root / "projected.parquet"
        completion = root / "completion.json"
        write_projected(projected, report, task, task_rows)
        address_map.build_map(
            projected,
            root,
            completion,
            execution_bucket=task["execution_bucket"],
            expected_release=RELEASE,
            expected_schema_fingerprint_sha256=report["schema_contract"][
                "fingerprint_sha256"
            ],
            expected_inventory_sha256=report["inventory_sha256"],
            expected_task_index=task["index"],
            expected_task_digest_sha256=task["task_digest_sha256"],
            expected_task_source_digest_sha256=task["source_digest_sha256"],
            maximum_hash_bits=maximum_hash_bits,
            scan_batch_rows=1,
            max_fragment_rows=1,
            max_fragment_bytes=100_000,
            max_rows=10,
        )
        result.append((completion, root))
    return result


def forge_first_serving_record(output: Path, mutation) -> Path:
    completion_path = (
        output / "families/addresses/reduce-completions/address-reduce-job-000.json"
    )
    completion = json.loads(completion_path.read_text())
    artifact = completion["artifacts"][0]
    index_path = output / artifact["index"]["relative_path"]
    data_path = output / artifact["data"]["relative_path"]
    entries = address_compression.read_index(index_path, max_bytes=20_000_000)
    original = data_path.read_bytes()
    header_end = (
        len(address_compression.DATA_MAGIC)
        + 4
        + struct.unpack_from("<I", original, len(address_compression.DATA_MAGIC))[0]
    )
    rewritten_data = bytearray(original[:header_end])
    rewritten_index = bytearray(address_compression.INDEX_MAGIC)
    accumulator = address_plan.SemanticAccumulator()
    changed = False
    for entry in entries:
        frame = original[entry["offset"] : entry["offset"] + entry["length"]]
        stored_length = struct.unpack_from("<I", frame)[0]
        records = address_compression.decode_page(
            gzip.decompress(frame[4 : 4 + stored_length]), useful=True
        )
        if not changed:
            mutation(records[0])
            changed = True
        raw = address_compression.encode_page(records, useful=True)
        stored = gzip.compress(raw, compresslevel=6, mtime=0)
        offset = len(rewritten_data)
        rewritten_data.extend(struct.pack("<I", len(stored)))
        rewritten_data.extend(stored)
        first_key = b"".join(
            address_compression.encode_text(value) for value in records[0]["key"][:8]
        )
        rewritten_index.extend(address_compression.encode_uvarint(offset))
        rewritten_index.extend(address_compression.encode_uvarint(4 + len(stored)))
        rewritten_index.extend(address_compression.encode_uvarint(len(records)))
        rewritten_index.extend(address_compression.encode_uvarint(len(first_key)))
        rewritten_index.extend(first_key)
        for record in records:
            accumulator.add(address_reduce.encode_record(record))
    assert changed
    data_path.write_bytes(rewritten_data)
    index_path.write_bytes(rewritten_index)
    for kind, path in (("data", data_path), ("index", index_path)):
        artifact[kind]["bytes"] = path.stat().st_size
        artifact[kind]["sha256"] = address_reduce.sha256_file(path)
    artifact["semantic_binding"] = accumulator.finish()
    completion["accounting"]["semantic_binding"] = (
        address_plan.combine_semantic_bindings(
            [item["semantic_binding"] for item in completion["artifacts"]],
            expected_records=completion["accounting"]["output_rows"],
        )
    )
    completion_path.write_bytes(address_plan.json_payload(completion))
    return completion_path


def semantic_binding(payload: bytes) -> dict:
    accumulator = address_plan.SemanticAccumulator()
    accumulator.add(payload)
    return accumulator.finish()


@pytest.fixture
def executor_fixture(tmp_path):
    report = canonical_inventory(tmp_path)
    matrix = map_matrix(tmp_path, report)
    output = tmp_path / "build"
    fanin = address_plan.build_fanin_plan(
        report,
        matrix,
        output,
        build_number=1,
        maximum_hash_bits=4,
        row_cap=3,
        max_reduce_jobs=1,
        max_source_fragments=32,
        page_rows=2,
        sparse_stride=1,
        max_page_rows=10,
    )
    return report, matrix, output, fanin


@pytest.mark.parametrize(
    ("arguments", "expected_option"),
    [
        (["run-job", "--help"], "--job-id"),
        (["finalize", "--help"], "--artifact-fetch-command-json"),
    ],
)
def test_address_reduce_cli_help_constructs_parser(
    monkeypatch, capsys, arguments, expected_option
):
    monkeypatch.setattr(sys, "argv", ["global_v2_address_reduce.py", *arguments])
    with pytest.raises(SystemExit) as raised:
        address_reduce.main()
    assert raised.value.code == 0
    assert expected_option in capsys.readouterr().out


def test_fanin_plan_validates_exact_matrix_counts_and_stable_jobs(executor_fixture):
    report, matrix, output, fanin = executor_fixture
    partition = json.loads(
        (output / "families/addresses/partition-plan.json").read_text()
    )
    reduce = json.loads((output / "families/addresses/reduce-plan.json").read_text())

    assert fanin["map_tasks"] == {
        "expected": 2,
        "completed": 2,
        "completion_set_sha256": partition["source"]["map_completion_set_sha256"],
    }
    assert partition["build"] == {
        "sequence": 1,
        "lineage_generation": 1,
        "predecessor": None,
    }
    assert reduce["partition_lineage"] == fanin["partition_lineage"] == {
        "schema": address_plan.PARTITION_LINEAGE_SCHEMA,
        "lineage_generation": 1,
        "predecessor": None,
    }
    assert partition["accounting"]["input_rows"] == 4
    assert partition["accounting"]["retained_rows"] == 4
    assert partition["accounting"]["rejected_rows"] == 0
    assert partition["accounting"]["exact_lookup_fanout"] == {
        "scope": "global",
        "status": "computed-by-exact-leaf-reducers",
        "task_maximum_lower_bound": 2,
    }
    assert reduce["totals"]["input_records"] == 4
    assert reduce["totals"]["expected_rows"] == 4
    assert reduce["totals"]["jobs"] == 1
    assert reduce["jobs"][0]["id"] == "address-reduce-job-000"
    assert reduce["jobs"][0]["is_serving_shard_id"] is False
    assert reduce["jobs"][0]["id"] not in reduce["jobs"][0]["partition_ids"]
    assert len(reduce["inputs"]) == 4
    assert all(item["object_key"] for item in reduce["inputs"])
    assert all((output / item["relative_path"]).is_file() for item in reduce["inputs"])


def test_reduce_compacts_bounded_spills_and_finalizes_worker_artifacts(
    executor_fixture,
):
    _, _, output, _ = executor_fixture
    partition_path = output / "families/addresses/partition-plan.json"
    reduce_path = output / "families/addresses/reduce-plan.json"
    completion = address_reduce.run_job(
        partition_path,
        reduce_path,
        job_id="address-reduce-job-000",
        input_root=output,
        output_root=output,
        sort_buffer_rows=1,
        sort_buffer_bytes=4096,
        max_open_files=5,
        max_workspace_bytes=100_000_000,
        max_artifact_bytes=20_000_000,
        max_shard_bytes=20_000_000,
    )

    assert completion["accounting"]["output_rows"] == 4
    assert completion["accounting"]["maximum_candidate_fanout"] == 3
    assert completion["accounting"]["peak_temporary_workspace_bytes"] > 0
    assert completion["partition_lineage"] == {
        "schema": address_plan.PARTITION_LINEAGE_SCHEMA,
        "lineage_generation": 1,
        "predecessor": None,
    }
    assert sum(item["rows"] for item in completion["artifacts"]) == 4
    for artifact in completion["artifacts"]:
        assert (output / artifact["index"]["relative_path"]).stat().st_size == artifact[
            "index"
        ]["bytes"]
        assert (output / artifact["data"]["relative_path"]).stat().st_size == artifact[
            "data"
        ]["bytes"]

    completion_path = (
        output / "families/addresses/reduce-completions/address-reduce-job-000.json"
    )
    tampered_completion_path = output / "tampered-lineage-completion.json"
    tampered_completion = json.loads(completion_path.read_text())
    tampered_completion["partition_lineage"]["lineage_generation"] = 2
    tampered_completion_path.write_text(json.dumps(tampered_completion))
    with pytest.raises(ValueError, match="completion identity"):
        address_reduce.finalize(
            partition_path,
            reduce_path,
            [tampered_completion_path],
            output_root=output,
        )
    final = address_reduce.finalize(
        partition_path,
        reduce_path,
        [completion_path],
        output_root=output,
    )
    assert final["accounting"] == {
        "retained_rows": 4,
        "final_shard_rows": 4,
        "serving_shards": len(completion["artifacts"]),
        "leaf_assignments_exactly_once": True,
        "final_shards_exactly_once": True,
        "exact_lookup_fanout": {
            "scope": "global",
            "maximum_candidates": 3,
            "distinct_lookup_keys": 2,
        },
    }
    collection = json.loads(
        (output / "families/addresses/address-collection.json").read_text()
    )
    assert final["partition_lineage"] == collection["partition_lineage"]
    assert final["partition_lineage"]["lineage_generation"] == 1
    assert collection["schema_version"] == 2
    assert collection["totals"]["retained_rows"] == 4

    ten_key = (
        "us",
        "ma",
        "stoneham",
        "stoneham",
        "02180",
        "main street",
        "10",
        "",
    )
    matching = next(
        item
        for item in collection["items"].values()
        if item["hash_start"] <= address_key_hash(ten_key) <= item["hash_end"]
    )
    candidates = indexed_lookup(
        output / matching["data_href"],
        output / matching["index_href"],
        ten_key,
        useful=True,
        compressed=True,
        max_index_bytes=20_000_000,
        max_page_bytes=8 * 1024 * 1024,
    )
    assert [candidate["id"] for candidate in candidates] == [
        str(uuid.UUID(int=value)) for value in (101, 102, 103)
    ]


def test_fanin_rejects_missing_duplicate_replayed_and_tampered_inputs(tmp_path):
    report = canonical_inventory(tmp_path)
    matrix = map_matrix(tmp_path, report)
    with pytest.raises(ValueError, match="missing|matrix"):
        address_plan.build_fanin_plan(
            report,
            matrix[:1],
            tmp_path / "missing",
            build_number=1,
            maximum_hash_bits=4,
            row_cap=3,
        )
    with pytest.raises(ValueError, match="unique|duplicated|replayed"):
        address_plan.build_fanin_plan(
            report,
            [matrix[0], matrix[0]],
            tmp_path / "duplicate",
            build_number=1,
            maximum_hash_bits=4,
            row_cap=3,
        )

    tampered_path = tmp_path / "tampered.json"
    tampered = json.loads(matrix[1][0].read_text())
    tampered["address_task_identity"]["task_digest_sha256"] = "0" * 64
    tampered_path.write_text(json.dumps(tampered))
    with pytest.raises(ValueError, match="canonical task"):
        address_plan.build_fanin_plan(
            report,
            [matrix[0], (tampered_path, matrix[1][1])],
            tmp_path / "tampered",
            build_number=1,
            maximum_hash_bits=4,
            row_cap=3,
        )


def test_predecessor_and_runtime_are_exact_and_remote_fetch_is_no_shell(
    executor_fixture, tmp_path
):
    report, matrix, output, _ = executor_fixture
    previous = json.loads(
        (output / "families/addresses/partition-plan.json").read_text()
    )
    previous_manifest = predecessor_family_manifest()
    with pytest.raises(ValueError, match="exact compatible"):
        address_plan.build_fanin_plan(
            report,
            matrix,
            tmp_path / "bad-predecessor",
            build_number=2,
            lineage_generation=2,
            predecessor_family_manifest=previous_manifest,
            previous_plan=previous,
            expected_previous_sha256="0" * 64,
            maximum_hash_bits=4,
            row_cap=3,
        )
    grown_source = tmp_path / "grown-source"
    grown_source.mkdir()
    grown_report = canonical_inventory(grown_source)
    grown_matrix = map_matrix(grown_source, grown_report, maximum_hash_bits=5)
    second = tmp_path / "second"
    address_plan.build_fanin_plan(
        grown_report,
        grown_matrix,
        second,
        build_number=1,
        lineage_generation=2,
        predecessor_family_manifest=previous_manifest,
        previous_plan=previous,
        expected_previous_sha256=address_plan.value_sha256(previous),
        maximum_hash_bits=5,
        row_cap=4,
        max_reduce_jobs=1,
    )
    current = json.loads(
        (second / "families/addresses/partition-plan.json").read_text()
    )
    assert current["build"]["predecessor"] == {
        "overture_release": RELEASE,
        "lineage_generation": 1,
        "partition_plan_sha256": address_plan.value_sha256(previous),
        "family_manifest": previous_manifest,
    }
    assert current["build"]["sequence"] == 1
    assert current["build"]["lineage_generation"] == 2
    assert current["partition"]["maximum_hash_bits"] == 5
    assert current["partition"]["split_row_cap"] == 4
    assert set(current["partition"]["split_ids"]).issuperset(
        previous["partition"]["split_ids"]
    )

    with pytest.raises(ValueError, match="maximum_hash_bits|compatible"):
        address_plan.build_fanin_plan(
            report,
            matrix,
            tmp_path / "decreased-bits",
            build_number=2,
            lineage_generation=2,
            predecessor_family_manifest=previous_manifest,
            previous_plan=previous,
            expected_previous_sha256=address_plan.value_sha256(previous),
            maximum_hash_bits=3,
            row_cap=4,
        )

    assert (
        address_plan.parse_fetch_command(
            '["wrangler","r2","object","get","bucket/{object_key}","--file","{output}"]'
        )[-1]
        == "{output}"
    )
    with pytest.raises(ValueError, match="JSON argv"):
        address_plan.parse_fetch_command("wrangler r2 object get")

    reduce_path = second / "families/addresses/reduce-plan.json"
    reduce = json.loads(reduce_path.read_text())
    reduce["serving_runtime_contract"]["zlib_runtime_version"] = "replayed"
    reduce_path.write_text(json.dumps(reduce, sort_keys=True) + "\n")
    with pytest.raises(ValueError, match="runtime-unpinned"):
        address_reduce.load_plans(
            second / "families/addresses/partition-plan.json", reduce_path
        )


def test_partition_lineage_rejects_bootstrap_skip_replay_and_partial_triples(
    executor_fixture, tmp_path
):
    report, matrix, output, _ = executor_fixture
    previous = json.loads(
        (output / "families/addresses/partition-plan.json").read_text()
    )
    previous_sha256 = address_plan.value_sha256(previous)
    manifest = predecessor_family_manifest()

    with pytest.raises(ValueError, match="exact predecessor triple"):
        address_plan.build_fanin_plan(
            report,
            matrix,
            tmp_path / "later-bootstrap",
            build_number=2,
            lineage_generation=2,
            predecessor_family_manifest=manifest,
            maximum_hash_bits=4,
            row_cap=3,
        )
    with pytest.raises(ValueError, match="generation 1.*null predecessor"):
        address_plan.build_fanin_plan(
            report,
            matrix,
            tmp_path / "replayed-bootstrap",
            build_number=2,
            lineage_generation=1,
            predecessor_family_manifest=manifest,
            previous_plan=previous,
            expected_previous_sha256=previous_sha256,
            maximum_hash_bits=4,
            row_cap=3,
        )
    with pytest.raises(ValueError, match="partially null"):
        address_plan.build_fanin_plan(
            report,
            matrix,
            tmp_path / "partial-triple",
            build_number=2,
            lineage_generation=2,
            predecessor_family_manifest={
                "object_key": manifest["object_key"],
                "bytes": None,
                "sha256": manifest["sha256"],
            },
            previous_plan=previous,
            expected_previous_sha256=previous_sha256,
            maximum_hash_bits=4,
            row_cap=3,
        )
    with pytest.raises(ValueError, match="exact compatible"):
        address_plan.build_fanin_plan(
            report,
            matrix,
            tmp_path / "skipped-generation",
            build_number=3,
            lineage_generation=3,
            predecessor_family_manifest=manifest,
            previous_plan=previous,
            expected_previous_sha256=previous_sha256,
            maximum_hash_bits=4,
            row_cap=3,
        )

    generation_two_root = tmp_path / "generation-two"
    address_plan.build_fanin_plan(
        report,
        matrix,
        generation_two_root,
        build_number=9,
        lineage_generation=2,
        predecessor_family_manifest=manifest,
        previous_plan=previous,
        expected_previous_sha256=previous_sha256,
        maximum_hash_bits=4,
        row_cap=4,
        max_reduce_jobs=1,
    )
    generation_two = json.loads(
        (generation_two_root / "families/addresses/partition-plan.json").read_text()
    )
    forged_boolean_generation = json.loads(json.dumps(generation_two))
    forged_boolean_generation["build"]["predecessor"]["lineage_generation"] = True
    with pytest.raises(ValueError, match="skipped or replayed"):
        address_plan.partition_lineage(forged_boolean_generation)
    with pytest.raises(ValueError, match="exact compatible"):
        address_plan.build_fanin_plan(
            report,
            matrix,
            tmp_path / "replayed-generation",
            build_number=10,
            lineage_generation=2,
            predecessor_family_manifest=predecessor_family_manifest(generation=2),
            previous_plan=generation_two,
            expected_previous_sha256=address_plan.value_sha256(generation_two),
            maximum_hash_bits=4,
            row_cap=4,
        )


def test_serialized_reduce_plan_cannot_bypass_worker_and_cardinality_caps(
    executor_fixture, monkeypatch
):
    _, _, output, _ = executor_fixture
    partition_path = output / "families/addresses/partition-plan.json"
    reduce_path = output / "families/addresses/reduce-plan.json"
    original = reduce_path.read_bytes()
    reduce = json.loads(original)
    reduce["serving_configuration"]["max_page_rows"] = (
        address_reduce.WORKER_MAX_PAGE_ROWS + 1
    )
    reduce_path.write_bytes(address_plan.json_payload(reduce))
    with pytest.raises(ValueError, match="runtime-unpinned"):
        address_reduce.load_plans(partition_path, reduce_path)

    reduce_path.write_bytes(original)
    monkeypatch.setattr(address_plan, "MAX_SOURCE_FRAGMENTS", 3)
    with pytest.raises(ValueError, match="input count exceeds"):
        address_reduce.load_plans(partition_path, reduce_path)
    monkeypatch.setattr(address_plan, "MAX_SOURCE_FRAGMENTS", 32)
    monkeypatch.setattr(address_plan, "MAX_SERVING_ROUTES", 1)
    with pytest.raises(ValueError, match="runtime-unpinned"):
        address_reduce.load_plans(partition_path, reduce_path)


def test_finalize_rejects_duplicate_completion(executor_fixture):
    _, _, output, _ = executor_fixture
    partition = output / "families/addresses/partition-plan.json"
    reduce = output / "families/addresses/reduce-plan.json"
    address_reduce.run_job(
        partition,
        reduce,
        job_id="address-reduce-job-000",
        input_root=output,
        output_root=output,
        sort_buffer_rows=2,
        sort_buffer_bytes=4096,
        max_open_files=5,
        max_workspace_bytes=100_000_000,
        max_artifact_bytes=20_000_000,
        max_shard_bytes=20_000_000,
    )
    completion = (
        output / "families/addresses/reduce-completions/address-reduce-job-000.json"
    )
    with pytest.raises(ValueError, match="missing or duplicated"):
        address_reduce.finalize(
            partition,
            reduce,
            [completion, completion],
            output_root=output,
        )


@pytest.mark.parametrize("artifact_kind", ["index", "data"])
def test_finalize_parses_serving_artifacts_after_forged_identity_update(
    executor_fixture, artifact_kind
):
    _, _, output, _ = executor_fixture
    partition = output / "families/addresses/partition-plan.json"
    reduce = output / "families/addresses/reduce-plan.json"
    address_reduce.run_job(
        partition,
        reduce,
        job_id="address-reduce-job-000",
        input_root=output,
        output_root=output,
        sort_buffer_rows=2,
        sort_buffer_bytes=4096,
        max_open_files=5,
        max_workspace_bytes=100_000_000,
        max_artifact_bytes=20_000_000,
        max_shard_bytes=20_000_000,
    )
    completion_path = (
        output / "families/addresses/reduce-completions/address-reduce-job-000.json"
    )
    original_completion_digest = hashlib.sha256(
        completion_path.read_bytes()
    ).hexdigest()
    completion = json.loads(completion_path.read_text())
    artifact = completion["artifacts"][0][artifact_kind]
    path = output / artifact["relative_path"]
    if artifact_kind == "index":
        path.write_bytes(b"forged-index-payload")
    else:
        # Preserve a wholly valid header and every indexed page, then forge a
        # new identity for unindexed bytes at the end of the data object.
        path.write_bytes(path.read_bytes() + b"forged-unindexed-data")
    artifact["bytes"] = path.stat().st_size
    artifact["sha256"] = address_reduce.sha256_file(path)
    completion_path.write_bytes(address_plan.json_payload(completion))
    assert hashlib.sha256(completion_path.read_bytes()).hexdigest() != (
        original_completion_digest
    )

    with pytest.raises(ValueError, match="address serving (index|data)"):
        address_reduce.finalize(
            partition,
            reduce,
            [completion_path],
            output_root=output,
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda record: record.__setitem__("country", "ZZ"), "display fields"),
        (lambda record: record.__setitem__("lon", -70.5), "semantic content"),
    ],
)
def test_finalize_rejects_semantic_substitution_with_recomputed_completion(
    executor_fixture, mutation, message
):
    _, _, output, _ = executor_fixture
    partition = output / "families/addresses/partition-plan.json"
    reduce = output / "families/addresses/reduce-plan.json"
    address_reduce.run_job(
        partition,
        reduce,
        job_id="address-reduce-job-000",
        input_root=output,
        output_root=output,
        sort_buffer_rows=2,
        sort_buffer_bytes=4096,
        max_open_files=5,
        max_workspace_bytes=100_000_000,
        max_artifact_bytes=20_000_000,
        max_shard_bytes=20_000_000,
    )
    completion = forge_first_serving_record(output, mutation)

    with pytest.raises(ValueError, match=message):
        address_reduce.finalize(
            partition,
            reduce,
            [completion],
            output_root=output,
        )


def test_remote_reduce_streams_one_content_addressed_fragment_at_a_time(tmp_path):
    report = canonical_inventory(tmp_path)
    matrix = map_matrix(tmp_path, report)
    output = tmp_path / "remote-build"
    address_plan.build_fanin_plan(
        report,
        matrix,
        output,
        build_number=1,
        maximum_hash_bits=4,
        row_cap=3,
        max_reduce_jobs=1,
        stage_local_fragments=False,
        page_rows=2,
        sparse_stride=1,
        max_page_rows=10,
    )
    reduce = json.loads((output / "families/addresses/reduce-plan.json").read_text())
    assert all("relative_path" not in item for item in reduce["inputs"])
    assert not (output / "families/addresses/reduce-inputs").exists()

    object_paths = {}
    for _, root in matrix:
        manifest = json.loads((root / "fragment-manifest.json").read_text())
        for fragment in manifest["fragments"]:
            object_paths[fragment["object_key"]] = str(root / fragment["relative_path"])
    mapping = tmp_path / "objects.json"
    mapping.write_text(json.dumps(object_paths))
    fetcher = tmp_path / "fetch.py"
    fetcher.write_text(
        "import json, shutil, sys\n"
        "mapping = json.load(open(sys.argv[1]))\n"
        "shutil.copyfile(mapping[sys.argv[2]], sys.argv[3])\n"
    )
    completion = address_reduce.run_job(
        output / "families/addresses/partition-plan.json",
        output / "families/addresses/reduce-plan.json",
        job_id="address-reduce-job-000",
        input_root=output,
        output_root=output,
        fragment_fetch_command=[
            sys.executable,
            str(fetcher),
            str(mapping),
            "{object_key}",
            "{output}",
        ],
        sort_buffer_rows=1,
        sort_buffer_bytes=4096,
        max_open_files=5,
        max_workspace_bytes=100_000_000,
        max_artifact_bytes=20_000_000,
        max_shard_bytes=20_000_000,
    )
    assert completion["accounting"]["output_rows"] == 4
    assert completion["accounting"]["peak_temporary_workspace_bytes"] >= max(
        item["bytes"] for item in reduce["inputs"]
    )
    with pytest.raises(ValueError, match="remote fragment exceeds"):
        address_reduce.run_job(
            output / "families/addresses/partition-plan.json",
            output / "families/addresses/reduce-plan.json",
            job_id="address-reduce-job-000",
            input_root=output,
            output_root=tmp_path / "remote-cap",
            fragment_fetch_command=[
                sys.executable,
                str(fetcher),
                str(mapping),
                "{object_key}",
                "{output}",
            ],
            sort_buffer_rows=1,
            sort_buffer_bytes=4096,
            max_open_files=5,
            max_workspace_bytes=min(item["bytes"] for item in reduce["inputs"]) - 1,
            max_artifact_bytes=20_000_000,
            max_shard_bytes=20_000_000,
        )


def test_finalize_fetches_and_deletes_one_verified_serving_pair_at_a_time(
    executor_fixture, tmp_path
):
    _, _, output, _ = executor_fixture
    partition = output / "families/addresses/partition-plan.json"
    reduce = output / "families/addresses/reduce-plan.json"
    completion = address_reduce.run_job(
        partition,
        reduce,
        job_id="address-reduce-job-000",
        input_root=output,
        output_root=output,
        sort_buffer_rows=2,
        sort_buffer_bytes=4096,
        max_open_files=5,
        max_workspace_bytes=100_000_000,
        max_artifact_bytes=20_000_000,
        max_shard_bytes=20_000_000,
    )
    remote = tmp_path / "remote-serving"
    remote.mkdir()
    objects = {}
    for artifact in completion["artifacts"]:
        for kind in ("index", "data"):
            identity = artifact[kind]
            source = output / identity["relative_path"]
            target = remote / identity["sha256"]
            shutil.move(source, target)
            objects[identity["relative_path"]] = target

    staged_parents = set()

    def corrupt_materialize(object_key: str, target: Path) -> None:
        staged_parents.add(target.parent)
        shutil.copyfile(objects[object_key], target)
        target.write_bytes(target.read_bytes() + b"corrupt")

    with pytest.raises(ValueError, match="content identity differs"):
        address_reduce.finalize(
            partition,
            reduce,
            [
                output
                / "families/addresses/reduce-completions/address-reduce-job-000.json"
            ],
            output_root=output,
            artifact_materializer=corrupt_materialize,
        )
    assert all(not path.exists() for path in staged_parents)
    staged_parents.clear()

    def materialize(object_key: str, target: Path) -> None:
        staged_parents.add(target.parent)
        assert len(list(target.parent.iterdir())) < 2
        shutil.copyfile(objects[object_key], target)

    final = address_reduce.finalize(
        partition,
        reduce,
        [output / "families/addresses/reduce-completions/address-reduce-job-000.json"],
        output_root=output,
        artifact_materializer=materialize,
    )

    expected_bytes = sum(
        artifact[kind]["bytes"]
        for artifact in completion["artifacts"]
        for kind in ("index", "data")
    )
    expected_peak = max(
        artifact["index"]["bytes"] + artifact["data"]["bytes"]
        for artifact in completion["artifacts"]
    )
    assert final["artifact_materialization"] == {
        "kind": "one-serving-pair-at-a-time-v1",
        "fetched_objects": len(completion["artifacts"]) * 2,
        "fetched_bytes": expected_bytes,
        "maximum_simultaneous_files": 2,
        "peak_simultaneous_staged_files": 2,
        "peak_staged_bytes": expected_peak,
        "exact_content_identity_verified": True,
    }
    assert all(not path.exists() for path in staged_parents)
    assert not list((output / "families/addresses/shards").glob("*.a*"))
    collection = json.loads(
        (output / "families/addresses/address-collection.json").read_text()
    )
    assert collection["artifact_materialization"] == final["artifact_materialization"]


def test_reduce_jobs_are_contiguous_balanced_and_limit_fragment_amplification():
    step = 1 << 55
    leaves = [
        {
            "id": f"a-us-h-{index:09b}",
            "country": "us",
            "hash_start": index * step,
            "hash_end": (index + 1) * step - 1,
            "rows": 1,
            "semantic_binding": semantic_binding(str(index).encode()),
        }
        for index in range(512)
    ]
    inputs = [
        {
            "index": index,
            "intermediate_ownership": {
                "country": "us",
                "hash_start": leaf["hash_start"],
                "hash_end": leaf["hash_end"],
            },
        }
        for index, leaf in enumerate(leaves)
    ]
    broad_index = len(inputs)
    inputs.append(
        {
            "index": broad_index,
            "intermediate_ownership": {
                "country": "us",
                "hash_start": leaves[200]["hash_start"],
                "hash_end": leaves[215]["hash_end"],
            },
        }
    )

    jobs = address_plan._assign_jobs(leaves, inputs, max_jobs=16)

    assert len(jobs) == 16
    assert {job["expected_rows"] for job in jobs} == {32}
    assert sum(broad_index in job["input_indexes"] for job in jobs) <= 2
    positions = {leaf["id"]: index for index, leaf in enumerate(leaves)}
    for job in jobs:
        assigned = [positions[identifier] for identifier in job["partition_ids"]]
        assert assigned == list(range(min(assigned), max(assigned) + 1))


def test_disk_backed_bucket_planning_preserves_sticky_splits_and_hard_caps(
    monkeypatch,
):
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE counts (country TEXT NOT NULL, bucket INTEGER NOT NULL, "
        "expected INTEGER NOT NULL, observed INTEGER NOT NULL DEFAULT 0, "
        "PRIMARY KEY (country, bucket)) WITHOUT ROWID"
    )
    connection.executemany(
        "INSERT INTO counts(country, bucket, expected) VALUES (?, ?, ?)",
        [("ca", 4, 1), ("us", 0, 3), ("us", 8, 2)],
    )
    statements = []
    connection.set_trace_callback(statements.append)
    first = address_plan.build_partition_plan_from_counts(
        connection,
        release=RELEASE,
        maximum_hash_bits=4,
        row_cap=3,
        previous=None,
    )
    assert [item["id"] for item in first["partitions"]] == [
        "a-ca",
        "a-us-h-0",
        "a-us-h-1",
    ]
    assert first["partition"]["split_ids"] == ["us:"]
    assert any("SUM(expected)" in statement for statement in statements)
    assert not any(
        "SELECT country, bucket, expected" in statement for statement in statements
    )

    grown = sqlite3.connect(":memory:")
    grown.execute(
        "CREATE TABLE counts (country TEXT NOT NULL, bucket INTEGER NOT NULL, "
        "expected INTEGER NOT NULL, observed INTEGER NOT NULL DEFAULT 0, "
        "PRIMARY KEY (country, bucket)) WITHOUT ROWID"
    )
    grown.executemany(
        "INSERT INTO counts(country, bucket, expected) VALUES (?, ?, ?)",
        [("ca", 8, 1), ("us", 0, 3), ("us", 16, 2)],
    )
    second = address_plan.build_partition_plan_from_counts(
        grown,
        release=RELEASE,
        maximum_hash_bits=5,
        row_cap=10,
        previous=first,
    )
    assert second["partition"]["split_ids"] == ["us:"]
    assert [item["id"] for item in second["partitions"]] == [
        "a-ca",
        "a-us-h-0",
        "a-us-h-1",
    ]

    monkeypatch.setattr(address_plan, "MAX_SERVING_ROUTES", 2)
    capped = sqlite3.connect(":memory:")
    capped.execute(
        "CREATE TABLE counts (country TEXT NOT NULL, bucket INTEGER NOT NULL, "
        "expected INTEGER NOT NULL, observed INTEGER NOT NULL DEFAULT 0, "
        "PRIMARY KEY (country, bucket)) WITHOUT ROWID"
    )
    capped.executemany(
        "INSERT INTO counts(country, bucket, expected) VALUES (?, ?, ?)",
        [("ca", 0, 1), ("mx", 0, 1), ("us", 0, 1)],
    )
    with pytest.raises(ValueError, match="serving-route hard cap"):
        address_plan.build_partition_plan_from_counts(
            capped,
            release=RELEASE,
            maximum_hash_bits=4,
            row_cap=10,
            previous=None,
        )


def test_fanin_streams_remote_fragments_one_at_a_time_with_exact_identity(tmp_path):
    report = canonical_inventory(tmp_path)
    matrix = map_matrix(tmp_path, report)
    remote = tmp_path / "remote"
    remote.mkdir()
    object_paths = {}
    for _, root in matrix:
        manifest = json.loads((root / "fragment-manifest.json").read_text())
        for fragment in manifest["fragments"]:
            source = root / fragment["relative_path"]
            target = remote / fragment["sha256"]
            shutil.move(source, target)
            object_paths[fragment["object_key"]] = str(target)
    mapping = tmp_path / "remote-objects.json"
    mapping.write_text(json.dumps(object_paths))
    fetcher = tmp_path / "bounded-fetch.py"
    fetcher.write_text(
        "import json, pathlib, shutil, sys\n"
        "mapping = json.load(open(sys.argv[1]))\n"
        "output = pathlib.Path(sys.argv[3])\n"
        "assert not list(output.parent.glob('*.bin'))\n"
        "shutil.copyfile(mapping[sys.argv[2]], output)\n"
    )
    output = tmp_path / "remote-fanin"
    fanin = address_plan.build_fanin_plan(
        report,
        matrix,
        output,
        build_number=1,
        maximum_hash_bits=4,
        row_cap=3,
        max_reduce_jobs=1,
        stage_local_fragments=False,
        fragment_fetch_command=[
            sys.executable,
            str(fetcher),
            str(mapping),
            "{object_key}",
            "{output}",
        ],
        bucket_db_cache_kib=1,
    )
    assert fanin["runtime"]["bucket_aggregation"] == {
        "kind": "disk-backed-exact-maximum-bucket-count-v1",
        "sqlite_runtime_version": sqlite3.sqlite_version,
        "cache_kib_at_most": 1,
    }
    reduce = json.loads((output / "families/addresses/reduce-plan.json").read_text())
    assert all("relative_path" not in item for item in reduce["inputs"])
    assert not list(output.glob(".address-fragment-slot-*"))

    corrupted = Path(next(iter(object_paths.values())))
    corrupted.write_bytes(corrupted.read_bytes() + b"corrupt")
    with pytest.raises(ValueError, match="changed during fan-in"):
        address_plan.build_fanin_plan(
            report,
            matrix,
            tmp_path / "corrupt-remote-fanin",
            build_number=1,
            maximum_hash_bits=4,
            row_cap=3,
            max_reduce_jobs=1,
            stage_local_fragments=False,
            fragment_fetch_command=[
                sys.executable,
                str(fetcher),
                str(mapping),
                "{object_key}",
                "{output}",
            ],
        )


def test_fanin_enforces_bucket_cache_and_source_fragment_hard_caps(tmp_path):
    report = canonical_inventory(tmp_path)
    matrix = map_matrix(tmp_path, report)
    with pytest.raises(ValueError, match="configuration|hard bounds"):
        address_plan.build_fanin_plan(
            report,
            matrix,
            tmp_path / "oversized-cache",
            build_number=1,
            maximum_hash_bits=4,
            row_cap=3,
            bucket_db_cache_kib=address_plan.MAX_BUCKET_DB_CACHE_KIB + 1,
        )
    with pytest.raises(ValueError, match="fragment count exceeds"):
        address_plan.build_fanin_plan(
            report,
            matrix,
            tmp_path / "too-many-fragments",
            build_number=1,
            maximum_hash_bits=4,
            row_cap=3,
            max_source_fragments=3,
        )
    with pytest.raises(ValueError, match="configuration.*hard bounds"):
        address_plan.build_fanin_plan(
            report,
            matrix,
            tmp_path / "bypassed-fragment-cap",
            build_number=1,
            maximum_hash_bits=4,
            row_cap=3,
            max_source_fragments=address_plan.MAX_SOURCE_FRAGMENTS + 1,
        )
