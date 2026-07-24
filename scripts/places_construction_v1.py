#!/usr/bin/env python3
"""Complete local Places construction-v1 map/genesis/reduce/head slice."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

# Serving index-hash domain, identical to the Rust encoder/verifier/Worker. The
# global head is split into `1 << shard_bits` shards keyed by the top bits of a
# token's index hash (12 bits => 4096 shards => first three hex nibbles),
# mirroring the production UUID-prefix ID index.
INDEX_DOMAIN = b"overture-places-serving-index-v1\0"
DEFAULT_HEAD_SHARD_BITS = 12


def index_hash(key: bytes) -> int:
    return int.from_bytes(hashlib.sha256(INDEX_DOMAIN + key).digest()[:8], "big")


def head_shard_of(token: str, shard_bits: int) -> int:
    if not 1 <= shard_bits <= 24:
        raise ValueError("head shard bits out of range")
    return index_hash(token.encode("utf-8")) >> (64 - shard_bits)


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "address_construction_shared", ROOT / "scripts/address_construction_v1.py"
)
assert SPEC and SPEC.loader
A = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = A
SPEC.loader.exec_module(A)

MARKER_SCHEMA = "overture-places-construction-task-marker-v1"
PLAN_SCHEMA = "overture-places-genesis-plan-v1"
REDUCE_SCHEMA = "overture-places-selective-reduce-v1"
TOTAL_ORDER = (
    "execution_group, partition_cell, token, confidence_rank DESC, feature_id, "
    "source_object_index, source_row_group, source_row_index"
)
HEAD_ORDER = (
    "token, confidence_rank DESC, feature_id, source_object_index, "
    "source_row_group, source_row_index"
)

# Single source of truth for the IPC batch-row cap. It mirrors the frozen
# evidence-spec `acceptance_gates.resources.maximum_ipc_batch_rows`; the
# readiness validator asserts the two cannot drift. Every hydrate, ingest, and
# write_arrow_query call reads this constant instead of a bare literal so the
# formerly independent call sites stay locked together.
MAX_IPC_BATCH_ROWS = 65_536
# Conservative upper bound on distinct tokens emitted per admitted feature. The
# authoritative Rust transform emits one output (term) batch per input batch, so
# a term batch holds at most `input_batch_rows * terms_per_feature` rows. The
# measured planet mean is ~14 terms/feature on the densest CJK tasks; 32 leaves
# better than 2x headroom over that mean while keeping the derived hydrate batch
# a round 2048 rows.
MAX_TERMS_PER_FEATURE = 32
# Derive the hydrate input batch from the IPC cap so a term batch stays under
# MAX_IPC_BATCH_ROWS by construction. ingest() still fail-closes if the bound is
# ever exceeded on pathological data.
HYDRATE_BATCH_ROWS = MAX_IPC_BATCH_ROWS // MAX_TERMS_PER_FEATURE
assert HYDRATE_BATCH_ROWS * MAX_TERMS_PER_FEATURE <= MAX_IPC_BATCH_ROWS


@dataclass(frozen=True)
class Limits:
    max_input_rows: int = 100_000
    max_pack_rows: int = 50_000
    parquet_row_group_rows: int = 2_048
    max_rss_bytes: int = 2 * 1024**3
    max_scratch_bytes: int = 4 * 1024**3
    max_output_bytes: int = 2 * 1024**3
    wall_seconds: float = 300
    duckdb_memory_limit: str = "1GB"
    duckdb_threads: int = 2
    required_duckdb_version: str = "1.5.1"
    allow_unpinned_duckdb: bool = False
    max_fan_in_tasks: int = 16
    max_fan_in_packs: int = 64
    partition_term_rows: int = 1_000_000
    partition_estimated_bytes: int = 268_435_456
    partition_distinct_tokens: int = 250_000
    adaptive_subdivision_depth: int = 8
    maximum_serving_candidates: int = 256
    head_result_cap: int = 10
    max_head_candidate_rows: int = 5_000_000
    require_bound_projection: bool = False

    def validate(self) -> None:
        if (
            any(
                value <= 0
                for value in (
                    self.max_input_rows,
                    self.max_pack_rows,
                    self.parquet_row_group_rows,
                    self.max_rss_bytes,
                    self.max_scratch_bytes,
                    self.max_output_bytes,
                    self.max_fan_in_tasks,
                    self.max_fan_in_packs,
                    self.partition_term_rows,
                    self.partition_estimated_bytes,
                    self.partition_distinct_tokens,
                    self.adaptive_subdivision_depth,
                    self.maximum_serving_candidates,
                    self.head_result_cap,
                    self.max_head_candidate_rows,
                )
            )
            or self.wall_seconds <= 0
        ):
            raise ValueError("Places construction limits must be positive")


def marker_key(task_id: str) -> str:
    if not task_id or any(
        c not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for c in task_id
    ):
        raise ValueError("unsafe Places task ID")
    return f"map/places-v1/tasks/{task_id}/complete.json"


def require_runtime(duckdb: Any, limits: Limits) -> None:
    if (
        duckdb.__version__ != limits.required_duckdb_version
        and not limits.allow_unpinned_duckdb
    ):
        raise RuntimeError(
            f"DuckDB {limits.required_duckdb_version} is required; found {duckdb.__version__}"
        )


def directory(
    binary: Path,
    arrow_path: Path,
    parquet_path: Path,
    output: Path,
    limits: Limits,
    roots: list[Path],
) -> tuple[dict[str, Any], dict[str, Any]]:
    layout = A.parquet_layout(parquet_path)
    layout_path = output.with_suffix(".layout.json")
    layout_path.write_text(json.dumps(layout, sort_keys=True) + "\n")
    evidence = A.run_bounded(
        [
            str(binary),
            "--input",
            str(arrow_path),
            "--layout",
            str(layout_path),
            "--output",
            str(output),
        ],
        scratch_roots=roots,
        limits=A.Limits(
            max_rss_bytes=limits.max_rss_bytes,
            max_scratch_bytes=limits.max_scratch_bytes,
            wall_seconds=limits.wall_seconds,
        ),
    )
    value = json.loads(output.read_text())
    if value.get("schema") != "overture-places-pack-proof-directory-v1":
        raise ValueError("Places proof directory schema differs")
    A.validate_binding(value["binding"])
    return value, evidence


def ingest(connection: Any, arrow_path: Path, table_name: str) -> dict[str, int | bool]:
    import pyarrow.ipc as ipc

    batches = 0
    maximum = 0
    with arrow_path.open("rb") as source:
        for batch in ipc.open_stream(source):
            if batch.num_rows > MAX_IPC_BATCH_ROWS:
                raise ValueError(
                    f"Places IPC batch exceeds {MAX_IPC_BATCH_ROWS} rows"
                )
            connection.register("places_batch", batch)
            try:
                if batches == 0:
                    connection.execute(
                        f"CREATE TABLE {table_name} AS SELECT * FROM places_batch WHERE false"
                    )
                connection.execute(
                    f"INSERT INTO {table_name} SELECT * FROM places_batch"
                )
            finally:
                connection.unregister("places_batch")
            batches += 1
            maximum = max(maximum, batch.num_rows)
            del batch
    if batches == 0:
        raise ValueError("Places IPC stream is empty")
    return {
        "batches": batches,
        "maximum_batch_rows": maximum,
        "full_table_read_all": False,
    }


def hydrate(
    input_path: Path, output: Path, *, batch_rows: int = HYDRATE_BATCH_ROWS
) -> dict[str, Any]:
    """Stream the flattened Places physical boundary to IPC without materializing it."""
    import pyarrow.ipc as ipc
    import pyarrow.parquet as pq

    parquet = pq.ParquetFile(input_path)
    batches = 0
    records = 0
    maximum = 0
    with output.open("wb") as destination:
        writer = None
        try:
            for batch in parquet.iter_batches(batch_size=batch_rows):
                if writer is None:
                    writer = ipc.new_stream(destination, batch.schema)
                writer.write_batch(batch)
                batches += 1
                records += batch.num_rows
                maximum = max(maximum, batch.num_rows)
        finally:
            if writer is not None:
                writer.close()
    if writer is None:
        raise ValueError("Places projected input is empty")
    return {
        "input_records": records,
        "input_row_groups": parquet.metadata.num_row_groups,
        "batches": batches,
        "maximum_batch_rows": maximum,
        "full_table_read_all": False,
    }


def validate_marker(
    marker: dict[str, Any], request: str, task_id: str, store: A.LocalObjectStore
):
    if (
        marker.get("schema") != MARKER_SCHEMA
        or marker.get("request_sha256") != request
        or marker.get("task_id") != task_id
        or not marker.get("packs")
    ):
        raise ValueError("Places marker identity differs")
    for pack in marker["packs"]:
        for identity in (pack["object"], pack["directory_object"]):
            path = store.path(identity["key"])
            if (
                not path.is_file()
                or path.stat().st_size != identity["bytes"]
                or A.sha256_file(path) != identity["sha256"]
            ):
                raise ValueError("Places immutable map object is missing or changed")
    head_candidates = marker.get("head_candidates")
    if not isinstance(head_candidates, dict):
        raise ValueError("Places marker is missing head candidates")
    identity = head_candidates.get("object")
    if not isinstance(identity, dict):
        raise ValueError("Places head candidate identity is invalid")
    path = store.path(identity["key"])
    if (
        not path.is_file()
        or path.stat().st_size != identity["bytes"]
        or A.sha256_file(path) != identity["sha256"]
    ):
        raise ValueError("Places immutable head candidates are missing or changed")
    if (
        A.combine_bindings([pack["directory"]["binding"] for pack in marker["packs"]])
        != marker["binding"]
    ):
        raise ValueError("Places marker binding differs from packs")
    return marker


def map_task(
    *,
    input_path: Path,
    source_limits: Path,
    store: A.LocalObjectStore,
    scratch_root: Path,
    request_sha256: str,
    task_id: str,
    transform_binary: Path,
    proof_binary: Path,
    limits: Limits,
    failpoint: str | None = None,
) -> dict[str, Any]:
    import duckdb
    import pyarrow.parquet as pq

    limits.validate()
    require_runtime(duckdb, limits)
    existing = store.read_json(marker_key(task_id))
    if existing is not None:
        return {
            **validate_marker(existing, request_sha256, task_id, store),
            "admitted_existing": True,
        }
    parquet = pq.ParquetFile(input_path)
    if parquet.metadata.num_rows > limits.max_input_rows:
        raise ValueError("Places input exceeds row cap")
    raw_identity = (parquet.schema_arrow.metadata or {}).get(
        b"overture.places_projection_identity"
    )
    projection_identity = json.loads(raw_identity) if raw_identity is not None else None
    if limits.require_bound_projection:
        if (
            not isinstance(projection_identity, dict)
            or projection_identity.get("schema")
            != "overture-places-construction-v1-physical-arrow-v1"
            or projection_identity.get("expected_input_records")
            != parquet.metadata.num_rows
            or not isinstance(projection_identity.get("inventory_sha256"), str)
            or not isinstance(projection_identity.get("inventory_file_sha256"), str)
            or not isinstance(projection_identity.get("schema_fingerprint_sha256"), str)
            or not isinstance(projection_identity.get("evidence_spec_sha256"), str)
            or projection_identity.get("task_index") is None
            or not projection_identity.get("task_digest")
            or not projection_identity.get("task_source_digest")
            or not projection_identity.get("ranges")
            or not projection_identity.get("objects")
        ):
            raise ValueError("Places projected input identity is missing or invalid")
    scratch_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f"places-{task_id}-", dir=scratch_root
    ) as name:
        workspace = Path(name)
        hydrated = workspace / "hydrated.arrow"
        with A.StageWatchdog(
            [workspace],
            A.Limits(
                max_rss_bytes=limits.max_rss_bytes,
                max_scratch_bytes=limits.max_scratch_bytes,
                wall_seconds=limits.wall_seconds,
            ),
        ) as hydration_watchdog:
            hydration = hydrate(input_path, hydrated)
        hydration_evidence = hydration_watchdog.evidence()
        transformed = workspace / "terms.arrow"
        transform_report_path = workspace / "transform.json"
        transform_evidence = A.run_bounded(
            [
                str(transform_binary),
                "--input",
                str(hydrated),
                "--output",
                str(transformed),
                "--report",
                str(transform_report_path),
                "--source-limits",
                str(source_limits),
            ],
            scratch_roots=[workspace],
            limits=A.Limits(
                max_rss_bytes=limits.max_rss_bytes,
                max_scratch_bytes=limits.max_scratch_bytes,
                wall_seconds=limits.wall_seconds,
            ),
        )
        transform = json.loads(transform_report_path.read_text())
        # Staged deletion (1/4): the hydrated stream is dead once the transform
        # has read it. Drop it before DuckDB ingest so it never coexists with the
        # term table.
        hydrated.unlink(missing_ok=True)
        if failpoint == "local_write":
            raise RuntimeError("injected Places interruption: local_write")
        database = workspace / "construction.duckdb"
        spill = workspace / "spill"
        spill.mkdir()
        connection = duckdb.connect(str(database))
        connection.execute(f"SET memory_limit='{limits.duckdb_memory_limit}'")
        connection.execute(f"SET threads={limits.duckdb_threads}")
        connection.execute(f"SET temp_directory='{spill}'")
        ingestion = ingest(connection, transformed, "terms")
        # Staged deletion (2/4): the term IPC stream is dead once DuckDB has
        # ingested it into the `terms` table. Drop it before any pack export so
        # the ~3 GiB IPC copy never coexists with the pack files.
        transformed.unlink(missing_ok=True)
        head_candidates_path = workspace / "head-candidates.parquet"
        connection.execute(
            f"COPY (SELECT * FROM terms QUALIFY row_number() OVER (PARTITION BY token "
            f"ORDER BY confidence_rank DESC, feature_id, source_object_index, "
            f"source_row_group, source_row_index)<={limits.head_result_cap} ORDER BY {HEAD_ORDER}) "
            f"TO '{head_candidates_path}' (FORMAT PARQUET, COMPRESSION ZSTD, COMPRESSION_LEVEL 6, "
            f"ROW_GROUP_SIZE {limits.parquet_row_group_rows}, PARQUET_VERSION V2, PRESERVE_ORDER true)"
        )
        head_candidate_rows = pq.ParquetFile(head_candidates_path).metadata.num_rows
        if head_candidate_rows > limits.max_head_candidate_rows:
            connection.close()
            raise ValueError("Places task head candidates exceed row cap")
        packs = []
        with A.StageWatchdog(
            [workspace],
            A.Limits(
                max_rss_bytes=limits.max_rss_bytes,
                max_scratch_bytes=limits.max_scratch_bytes,
                wall_seconds=limits.wall_seconds,
            ),
            connection,
        ) as watchdog:
            # Write the single pack-tagged copy the map contract requires ONCE, as
            # an on-disk parquet rather than a second in-database table. A zstd
            # parquet is several times smaller than the equivalent DuckDB table,
            # and `terms` is dropped the instant the copy exists, so two full copies
            # never coexist at DuckDB-table size -- this is what keeps the dense-task
            # scratch peak inside the cap. The ~3 GiB term IPC is already gone
            # (staged deletion 2/4).
            #
            # Exactly ONE sort runs here: the window `row_number() OVER (ORDER BY
            # TOTAL_ORDER)` that assigns pack_id. The copy is deliberately NOT given
            # an outer `ORDER BY` -- that would be a second full external sort of
            # ~14M rows, doubling the spill and blowing the scratch cap. Each pack
            # is re-sorted by TOTAL_ORDER when it is extracted below, and TOTAL_ORDER
            # is a total order, so every pack is byte- and order-identical to the
            # established per-pack-from-table pipeline regardless of the copy's
            # physical order.
            #
            # A single `COPY ... PARTITION_BY (pack_id)` pass cannot be used to write
            # the packs directly: DuckDB does NOT preserve row order within a
            # partition (PRESERVE_ORDER is rejected with PARTITION_BY), so the pack
            # files would land unsorted and the sorted-pack row-group proof that
            # reduce reconciles against would break.
            connection.execute("SET threads=1")
            packed_parquet = workspace / "packed.parquet"
            connection.execute(
                f"COPY (SELECT *, ((row_number() OVER (ORDER BY {TOTAL_ORDER})-1) "
                f"// {limits.max_pack_rows})::UINTEGER pack_id FROM terms) "
                f"TO '{packed_parquet}' (FORMAT PARQUET, COMPRESSION ZSTD, "
                f"COMPRESSION_LEVEL 6, ROW_GROUP_SIZE {limits.parquet_row_group_rows}, "
                "PARQUET_VERSION V2)"
            )
            # Staged deletion (3/4): `terms` is dead once the copy exists. Drop it
            # and CHECKPOINT so its blocks are reclaimed before the per-pack files
            # begin to accumulate.
            connection.execute("DROP TABLE terms")
            connection.execute("CHECKPOINT")
            packed_source = f"read_parquet('{packed_parquet}')"
            pack_count = connection.execute(
                f"SELECT coalesce(max(pack_id)+1,0)::UINTEGER FROM {packed_source}"
            ).fetchone()[0]
            for pack_id in range(pack_count):
                pack = workspace / f"pack-{pack_id:06d}.parquet"
                connection.execute(
                    f"COPY (SELECT * EXCLUDE(pack_id) FROM {packed_source} WHERE pack_id={pack_id} "
                    f"ORDER BY {TOTAL_ORDER}) TO '{pack}' (FORMAT PARQUET, COMPRESSION ZSTD, "
                    f"COMPRESSION_LEVEL 6, ROW_GROUP_SIZE {limits.parquet_row_group_rows}, "
                    "PARQUET_VERSION V2, PRESERVE_ORDER true)"
                )
                ordered = workspace / f"pack-{pack_id:06d}.arrow"
                rows = A.write_arrow_query(
                    connection,
                    f"SELECT * EXCLUDE(pack_id) FROM {packed_source} WHERE pack_id={pack_id} ORDER BY {TOTAL_ORDER}",
                    ordered,
                    MAX_IPC_BATCH_ROWS,
                )
                proof_path = workspace / f"pack-{pack_id:06d}.directory.json"
                proof, proof_evidence = directory(
                    proof_binary, ordered, pack, proof_path, limits, [workspace]
                )
                if rows != proof["binding"]["records"]:
                    raise ValueError("Places pack proof row count differs")
                if pack.stat().st_size > limits.max_output_bytes:
                    raise ValueError("Places pack exceeds output cap")
                packs.append(
                    {
                        "pack_id": pack_id,
                        "object": store.put_content(
                            pack, "map/places-v1/packs", ".parquet"
                        ),
                        "directory_object": store.put_content(
                            proof_path, "map/places-v1/directories", ".json"
                        ),
                        "directory": proof,
                        "proof_evidence": proof_evidence,
                    }
                )
                # Staged deletion (4/4): store.put_content already copied the pack
                # bytes out, so the workspace originals are dead weight. Unlink each
                # immediately to keep the retained-pack set from accumulating.
                pack.unlink(missing_ok=True)
                ordered.unlink(missing_ok=True)
        construction_evidence = watchdog.evidence()
        connection.close()
        head_candidates = {
            "schema": "overture-places-map-head-candidates-v1",
            "result_cap": limits.head_result_cap,
            "records": head_candidate_rows,
            "object": store.put_content(
                head_candidates_path, "map/places-v1/head-candidates", ".parquet"
            ),
        }
        if failpoint in {"after_objects", "before_marker"}:
            raise RuntimeError(f"injected Places interruption: {failpoint}")
        binding = A.combine_bindings([pack["directory"]["binding"] for pack in packs])
        if (
            binding["records"] != transform["emitted_term_rows"]
            or binding["semantic_sum_a"] != transform["semantic_sum_a"]
            or binding["semantic_sum_b"] != transform["semantic_sum_b"]
        ):
            raise ValueError("Places map binding differs from transform")
        marker = {
            "schema": MARKER_SCHEMA,
            "binding_schema": A.BINDING_SCHEMA,
            "request_sha256": request_sha256,
            "task_id": task_id,
            "input": {
                "sha256": A.sha256_file(input_path),
                "bytes": input_path.stat().st_size,
                "features": parquet.metadata.num_rows,
            },
            "source_limits_sha256": A.sha256_file(source_limits),
            "projection_identity": projection_identity,
            "limits": asdict(limits),
            "hydration": hydration,
            "hydration_evidence": hydration_evidence,
            "transform": transform,
            "transform_evidence": transform_evidence,
            "ingestion": ingestion,
            "construction_evidence": construction_evidence,
            "packs": packs,
            "head_candidates": head_candidates,
            "binding": binding,
        }
        store.write_marker_last(marker_key(task_id), marker)
        return {**marker, "admitted_existing": False}


def genesis_plan(markers: list[dict[str, Any]], *, row_cap: int) -> dict[str, Any]:
    if row_cap <= 0:
        raise ValueError("Places partition row cap must be positive")
    by_cell: dict[str, list[dict[str, Any]]] = {}
    for marker in markers:
        for pack in marker["packs"]:
            for summary in pack["directory"]["routing_summaries"]:
                by_cell.setdefault(summary["partition_cell"], []).append(
                    summary["binding"]
                )
    partitions = []
    for cell in sorted(by_cell):
        binding = A.combine_bindings(by_cell[cell])
        if binding["records"] > row_cap:
            raise ValueError("Places cell exceeds provisional unsplittable row cap")
        partitions.append(
            {
                "id": f"p-{cell}",
                "execution_group": cell[:2],
                "partition_cell": cell,
                "binding": binding,
            }
        )
    return {
        "schema": PLAN_SCHEMA,
        "predecessor": None,
        "marker_task_ids": sorted(marker["task_id"] for marker in markers),
        "row_cap": row_cap,
        "partitions": partitions,
        "binding": A.combine_bindings([item["binding"] for item in partitions]),
    }


def _sql_paths(paths: list[Path]) -> str:
    return ",".join("'" + str(path).replace("'", "''") + "'" for path in paths)


def _term_bytes_sql() -> str:
    fields = [
        "execution_group",
        "partition_cell",
        "token",
        "primary_name",
        "brand_name",
        "category",
        "locality",
        "region",
        "country",
    ]
    return "96+" + "+".join(f"octet_length(encode({field}))" for field in fields)


def _prefix_sql(depth: int) -> str:
    if depth <= 0 or depth > 8:
        raise ValueError("Places adaptive prefix depth is invalid")
    return f"(token_hash >> {64 - depth * 4})"


def _partition_mask(table: Any, partition: dict[str, Any]) -> Any:
    import pyarrow as pa
    import pyarrow.compute as pc

    mask = pc.equal(table["partition_cell"], partition["partition_cell"])
    ownership = partition.get("ownership", {"depth": 0, "prefix": 0})
    if ownership["depth"]:
        shift = 64 - ownership["depth"] * 4
        hashes = pc.shift_right(table["token_hash"], pa.scalar(shift, pa.uint64()))
        mask = pc.and_(
            mask, pc.equal(hashes, pa.scalar(ownership["prefix"], pa.uint64()))
        )
    return mask


def adaptive_genesis_plan(
    markers: list[dict[str, Any]],
    *,
    store: A.LocalObjectStore,
    scratch_root: Path,
    limits: Limits,
) -> dict[str, Any]:
    """Build a predecessor-free adaptive plan from bounded, on-disk pack scans."""
    import duckdb
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    limits.validate()
    if not markers or len(markers) > limits.max_fan_in_tasks:
        raise ValueError("Places adaptive genesis task fan-in exceeds cap")
    task_ids = [marker["task_id"] for marker in markers]
    if len(task_ids) != len(set(task_ids)):
        raise ValueError("Places adaptive genesis contains duplicate tasks")
    packs = [pack for marker in markers for pack in marker["packs"]]
    if not packs:
        raise ValueError("Places adaptive genesis contains no packs")
    paths = [store.path(pack["object"]["key"]) for pack in packs]
    scratch_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="places-genesis-", dir=scratch_root
    ) as name:
        workspace = Path(name)
        connection = duckdb.connect(str(workspace / "genesis.duckdb"))
        connection.execute(f"SET memory_limit='{limits.duckdb_memory_limit}'")
        connection.execute(f"SET threads={limits.duckdb_threads}")
        connection.execute(f"SET temp_directory='{workspace}'")
        byte_expression = _term_bytes_sql()
        for start in range(0, len(paths), limits.max_fan_in_packs):
            batch_paths = paths[start : start + limits.max_fan_in_packs]
            statement = (
                "CREATE TABLE planning AS" if start == 0 else "INSERT INTO planning"
            )
            connection.execute(
                f"{statement} SELECT partition_cell, token, token_hash, "
                f"({byte_expression})::UBIGINT estimated_bytes FROM "
                f"read_parquet([{_sql_paths(batch_paths)}])"
            )
        source = "planning"
        byte_sql = "estimated_bytes"
        cells = connection.execute(
            f"SELECT partition_cell, count(*)::UBIGINT, sum({byte_sql})::UBIGINT, "
            f"count(DISTINCT token)::UBIGINT FROM {source} GROUP BY partition_cell "
            "ORDER BY partition_cell"
        ).fetchall()
        partitions: list[dict[str, Any]] = []

        def add(
            cell: str, depth: int, prefix: int, rows: int, size: int, tokens: int
        ) -> None:
            over = (
                rows > limits.partition_term_rows
                or size > limits.partition_estimated_bytes
                or tokens > limits.partition_distinct_tokens
            )
            if not over:
                suffix = "" if depth == 0 else f"-h{prefix:0{depth}x}"
                partitions.append(
                    {
                        "id": f"p-{cell}{suffix}",
                        "execution_group": cell[:2],
                        "partition_cell": cell,
                        "ownership": {
                            "kind": "token-sha256-nibble-prefix-v1",
                            "depth": depth,
                            "prefix": prefix,
                        },
                        "term_rows": rows,
                        "estimated_uncompressed_bytes": size,
                        "distinct_tokens": tokens,
                    }
                )
                return
            if depth >= limits.adaptive_subdivision_depth:
                raise ValueError(
                    "Places adaptive partition remains over cap at maximum depth"
                )
            next_depth = depth + 1
            prefix_expression = _prefix_sql(next_depth)
            where = f"partition_cell='{cell}'"
            if depth:
                where += f" AND {_prefix_sql(depth)}={prefix}"
            children = connection.execute(
                f"SELECT {prefix_expression}::UBIGINT child, count(*)::UBIGINT, "
                f"sum({byte_sql})::UBIGINT, count(DISTINCT token)::UBIGINT "
                f"FROM {source} WHERE {where} GROUP BY child ORDER BY child"
            ).fetchall()
            if not children:
                raise ValueError("Places adaptive subdivision produced no children")
            for child, child_rows, child_size, child_tokens in children:
                add(
                    cell,
                    next_depth,
                    int(child),
                    int(child_rows),
                    int(child_size),
                    int(child_tokens),
                )

        for cell, rows, size, tokens in cells:
            add(cell, 0, 0, int(rows), int(size), int(tokens))
        connection.close()

        accumulators = [
            {"records": 0, "semantic_sum_a": 0, "semantic_sum_b": 0} for _ in partitions
        ]
        by_cell: dict[str, list[int]] = {}
        for index, partition in enumerate(partitions):
            by_cell.setdefault(partition["partition_cell"], []).append(index)
        for path in paths:
            parquet = pq.ParquetFile(path)
            for batch in parquet.iter_batches(
                batch_size=MAX_IPC_BATCH_ROWS,
                columns=[
                    "partition_cell",
                    "token_hash",
                    "semantic_digest_a",
                    "semantic_digest_b",
                ],
            ):
                table = pa.Table.from_batches([batch])
                for cell in set(table["partition_cell"].to_pylist()):
                    cell_table = table.filter(pc.equal(table["partition_cell"], cell))
                    for index in by_cell[cell]:
                        selected = cell_table.filter(
                            _partition_mask(cell_table, partitions[index])
                        )
                        binding = binding_for_table(selected)
                        accumulator = accumulators[index]
                        accumulator["records"] += binding["records"]
                        accumulator["semantic_sum_a"] = (
                            accumulator["semantic_sum_a"]
                            + int(binding["semantic_sum_a"], 16)
                        ) % A.UINT256
                        accumulator["semantic_sum_b"] = (
                            accumulator["semantic_sum_b"]
                            + int(binding["semantic_sum_b"], 16)
                        ) % A.UINT256
        for partition, accumulator in zip(partitions, accumulators, strict=True):
            partition["binding"] = {
                "records": accumulator["records"],
                "semantic_sum_a": f"{accumulator['semantic_sum_a']:064x}",
                "semantic_sum_b": f"{accumulator['semantic_sum_b']:064x}",
            }
            if partition["binding"]["records"] != partition["term_rows"]:
                raise ValueError("Places adaptive partition proof row count differs")
        plan = {
            "schema": "overture-places-adaptive-genesis-plan-v1",
            "predecessor": None,
            "marker_task_ids": sorted(task_ids),
            "limits": {
                "term_rows": limits.partition_term_rows,
                "estimated_uncompressed_bytes": limits.partition_estimated_bytes,
                "distinct_tokens": limits.partition_distinct_tokens,
                "maximum_depth": limits.adaptive_subdivision_depth,
                "maximum_fan_in_tasks": limits.max_fan_in_tasks,
                "maximum_fan_in_packs": limits.max_fan_in_packs,
            },
            "partitions": partitions,
            "binding": A.combine_bindings([item["binding"] for item in partitions]),
        }
        expected = A.combine_bindings([marker["binding"] for marker in markers])
        if plan["binding"] != expected:
            raise ValueError("Places adaptive genesis binding differs from map markers")
        return plan


def binding_for_table(table: Any) -> dict[str, Any]:
    total_a = 0
    total_b = 0
    for chunk in table["semantic_digest_a"].chunks:
        total_a = (
            total_a + sum(int.from_bytes(value.as_py(), "big") for value in chunk)
        ) % A.UINT256
    for chunk in table["semantic_digest_b"].chunks:
        total_b = (
            total_b + sum(int.from_bytes(value.as_py(), "big") for value in chunk)
        ) % A.UINT256
    return {
        "records": table.num_rows,
        "semantic_sum_a": f"{total_a:064x}",
        "semantic_sum_b": f"{total_b:064x}",
    }


def reduce_partition(
    *,
    partition: dict[str, Any],
    plan: dict[str, Any],
    markers: list[dict[str, Any]],
    store: A.LocalObjectStore,
    scratch_root: Path,
    encoder_binary: Path,
    verifier_binary: Path,
    limits: Limits,
) -> dict[str, Any]:
    import duckdb
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    if sorted(marker["task_id"] for marker in markers) != plan["marker_task_ids"]:
        raise ValueError("Places reducer marker set is missing or extra")
    scratch_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f"reduce-{partition['id']}-", dir=scratch_root
    ) as name:
        workspace = Path(name)
        spill = workspace / "spill"
        spill.mkdir()
        connection = duckdb.connect(str(workspace / "reduce.duckdb"))
        connection.execute(f"SET memory_limit='{limits.duckdb_memory_limit}'")
        connection.execute(f"SET threads={limits.duckdb_threads}")
        connection.execute(f"SET temp_directory='{spill}'")
        reconciled = []
        selected_bindings = []
        initialized = False
        maximum_batch_rows = 0
        for marker in markers:
            for pack in marker["packs"]:
                selected_groups = [
                    row_group["index"]
                    for row_group in pack["directory"]["row_groups"]
                    if any(
                        group["partition_cell"] == partition["partition_cell"]
                        for group in row_group["routing_groups"]
                    )
                ]
                if not selected_groups:
                    continue
                parquet = pq.ParquetFile(store.path(pack["object"]["key"]))
                for index in selected_groups:
                    group_selected = []
                    group_discarded = []
                    for batch in parquet.iter_batches(
                        batch_size=MAX_IPC_BATCH_ROWS,
                        row_groups=[index],
                        use_threads=False,
                    ):
                        maximum_batch_rows = max(maximum_batch_rows, batch.num_rows)
                        mask = _partition_mask(batch, partition)
                        selected = batch.filter(mask)
                        discarded = batch.filter(pc.invert(mask))
                        group_selected.append(
                            binding_for_table(pa.Table.from_batches([selected]))
                        )
                        group_discarded.append(
                            binding_for_table(pa.Table.from_batches([discarded]))
                        )
                        if not initialized:
                            connection.register("places_selected_batch", batch)
                            connection.execute(
                                "CREATE TABLE selected AS SELECT * FROM places_selected_batch WHERE false"
                            )
                            connection.unregister("places_selected_batch")
                            initialized = True
                        if selected.num_rows:
                            connection.register("places_selected_batch", selected)
                            try:
                                connection.execute(
                                    "INSERT INTO selected SELECT * FROM places_selected_batch"
                                )
                            finally:
                                connection.unregister("places_selected_batch")
                    selected_binding = A.combine_bindings(group_selected)
                    discarded_binding = A.combine_bindings(group_discarded)
                    combined = A.combine_bindings([selected_binding, discarded_binding])
                    expected = pack["directory"]["row_groups"][index]["binding"]
                    if combined != expected:
                        raise ValueError(
                            "Places selected/discarded row-group proof differs"
                        )
                    selected_bindings.append(selected_binding)
                    reconciled.append(
                        {
                            "pack_sha256": pack["object"]["sha256"],
                            "row_group": index,
                            "selected": selected_binding,
                            "discarded": discarded_binding,
                        }
                    )
        if not initialized:
            connection.close()
            raise ValueError("Places reducer selected no row groups")
        selected_binding = A.combine_bindings(selected_bindings)
        if selected_binding != partition["binding"]:
            connection.close()
            raise ValueError("Places reducer selected binding differs from plan")
        leaf = workspace / "leaf.parquet"
        connection.execute(
            f"COPY (SELECT * FROM selected ORDER BY {TOTAL_ORDER}) TO '{leaf}' "
            f"(FORMAT PARQUET, COMPRESSION ZSTD, COMPRESSION_LEVEL 6, "
            f"ROW_GROUP_SIZE {limits.parquet_row_group_rows}, PARQUET_VERSION V2, PRESERVE_ORDER true)"
        )
        arrow = workspace / "leaf.arrow"
        serving_rows = A.write_arrow_query(
            connection,
            f"SELECT * FROM selected QUALIFY row_number() OVER (PARTITION BY partition_cell, token "
            f"ORDER BY confidence_rank DESC, feature_id, source_object_index, source_row_group, "
            f"source_row_index)<={limits.maximum_serving_candidates} ORDER BY {TOTAL_ORDER}",
            arrow,
            MAX_IPC_BATCH_ROWS,
        )
        connection.close()
        routed = workspace / "routed.plrv"
        encode_evidence = A.run_bounded(
            [
                str(encoder_binary),
                "--input",
                str(arrow),
                "--output",
                str(routed),
                "--mode",
                "routed",
            ],
            scratch_roots=[workspace],
            limits=A.Limits(
                max_rss_bytes=limits.max_rss_bytes,
                max_scratch_bytes=limits.max_scratch_bytes,
                wall_seconds=limits.wall_seconds,
            ),
        )
        A.run_bounded(
            [str(verifier_binary), "--input", str(routed), "--mode", "routed"],
            scratch_roots=[workspace],
            limits=A.Limits(
                max_rss_bytes=limits.max_rss_bytes,
                max_scratch_bytes=limits.max_scratch_bytes,
                wall_seconds=limits.wall_seconds,
            ),
        )
        if (
            leaf.stat().st_size > limits.max_output_bytes
            or routed.stat().st_size > limits.max_output_bytes
        ):
            raise ValueError("Places reduce output exceeds cap")
        return {
            "schema": REDUCE_SCHEMA,
            "partition": partition,
            "binding": selected_binding,
            "reconciled_row_groups": reconciled,
            "streaming_ingestion": {
                "maximum_batch_rows": maximum_batch_rows,
                "full_table_read_all": False,
            },
            "serving_candidate_rows": serving_rows,
            "leaf_object": store.put_content(
                leaf, "reduce/places-v1/leaves", ".parquet"
            ),
            "routed_object": store.put_content(
                routed, "serve/places-v1/routed", ".plrv"
            ),
            "encode_evidence": encode_evidence,
        }


def build_global_head(
    *,
    reductions: list[dict[str, Any]],
    store: A.LocalObjectStore,
    scratch_root: Path,
    encoder_binary: Path,
    verifier_binary: Path,
    limits: Limits,
    result_cap: int = 10,
) -> dict[str, Any]:
    import duckdb

    if result_cap <= 0:
        raise ValueError("Places head result cap must be positive")
    scratch_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="places-head-", dir=scratch_root) as name:
        workspace = Path(name)
        connection = duckdb.connect(str(workspace / "head.duckdb"))
        connection.execute(f"SET memory_limit='{limits.duckdb_memory_limit}'")
        connection.execute(f"SET threads={limits.duckdb_threads}")
        paths = [str(store.path(item["leaf_object"]["key"])) for item in reductions]
        literals = ",".join("'" + path.replace("'", "''") + "'" for path in paths)
        arrow = workspace / "head.arrow"
        A.write_arrow_query(
            connection,
            f"SELECT * FROM (SELECT *, row_number() OVER (PARTITION BY token ORDER BY "
            f"confidence_rank DESC, feature_id, source_object_index, source_row_group, "
            f"source_row_index) AS head_position FROM read_parquet([{literals}])) "
            f"WHERE head_position<={result_cap} "
            f"ORDER BY {HEAD_ORDER}",
            arrow,
            MAX_IPC_BATCH_ROWS,
        )
        connection.close()
        head = workspace / "head.plhd"
        encode = A.run_bounded(
            [
                str(encoder_binary),
                "--input",
                str(arrow),
                "--output",
                str(head),
                "--mode",
                "head",
            ],
            scratch_roots=[workspace],
            limits=A.Limits(
                max_rss_bytes=limits.max_rss_bytes,
                max_scratch_bytes=limits.max_scratch_bytes,
                wall_seconds=limits.wall_seconds,
            ),
        )
        A.run_bounded(
            [str(verifier_binary), "--input", str(head), "--mode", "head"],
            scratch_roots=[workspace],
            limits=A.Limits(
                max_rss_bytes=limits.max_rss_bytes,
                max_scratch_bytes=limits.max_scratch_bytes,
                wall_seconds=limits.wall_seconds,
            ),
        )
        if head.stat().st_size > limits.max_output_bytes:
            raise ValueError("Places head output exceeds cap")
        binding = A.combine_bindings([item["binding"] for item in reductions])
        return {
            "schema": "overture-places-global-head-v1",
            "result_cap": result_cap,
            "input_binding": binding,
            "head_object": store.put_content(head, "serve/places-v1/head", ".plhd"),
            "encode_evidence": encode,
        }


def build_global_head_from_markers(
    *,
    markers: list[dict[str, Any]],
    store: A.LocalObjectStore,
    scratch_root: Path,
    encoder_binary: Path,
    verifier_binary: Path,
    limits: Limits,
) -> dict[str, Any]:
    """Merge only bounded per-task candidates, never planet term leaves."""
    import duckdb

    if not markers or len(markers) > limits.max_fan_in_tasks:
        raise ValueError("Places head task fan-in exceeds cap")
    task_ids = [marker["task_id"] for marker in markers]
    if len(task_ids) != len(set(task_ids)):
        raise ValueError("Places head contains duplicate map tasks")
    candidates = [marker["head_candidates"] for marker in markers]
    if any(item["result_cap"] != limits.head_result_cap for item in candidates):
        raise ValueError("Places task head cap differs")
    input_rows = sum(item["records"] for item in candidates)
    if input_rows > limits.max_head_candidate_rows:
        raise ValueError("Places merged head candidates exceed row cap")
    paths = [store.path(item["object"]["key"]) for item in candidates]
    scratch_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="places-head-merge-", dir=scratch_root
    ) as name:
        workspace = Path(name)
        connection = duckdb.connect(str(workspace / "head.duckdb"))
        connection.execute(f"SET memory_limit='{limits.duckdb_memory_limit}'")
        connection.execute(f"SET threads={limits.duckdb_threads}")
        connection.execute(f"SET temp_directory='{workspace}'")
        arrow = workspace / "head.arrow"
        output_rows = A.write_arrow_query(
            connection,
            f"SELECT * FROM read_parquet([{_sql_paths(paths)}]) QUALIFY row_number() OVER "
            f"(PARTITION BY token ORDER BY confidence_rank DESC, feature_id, "
            f"source_object_index, source_row_group, source_row_index)<={limits.head_result_cap} "
            f"ORDER BY {HEAD_ORDER}",
            arrow,
            MAX_IPC_BATCH_ROWS,
        )
        connection.close()
        head = workspace / "head.plhd"
        encode = A.run_bounded(
            [
                str(encoder_binary),
                "--input",
                str(arrow),
                "--output",
                str(head),
                "--mode",
                "head",
            ],
            scratch_roots=[workspace],
            limits=A.Limits(
                max_rss_bytes=limits.max_rss_bytes,
                max_scratch_bytes=limits.max_scratch_bytes,
                wall_seconds=limits.wall_seconds,
            ),
        )
        verify = A.run_bounded(
            [str(verifier_binary), "--input", str(head), "--mode", "head"],
            scratch_roots=[workspace],
            limits=A.Limits(
                max_rss_bytes=limits.max_rss_bytes,
                max_scratch_bytes=limits.max_scratch_bytes,
                wall_seconds=limits.wall_seconds,
            ),
        )
        if head.stat().st_size > limits.max_output_bytes:
            raise ValueError("Places merged head exceeds output cap")
        return {
            "schema": "overture-places-global-head-v2",
            "task_ids": sorted(task_ids),
            "result_cap": limits.head_result_cap,
            "input_candidate_rows": input_rows,
            "output_rows": output_rows,
            "input_binding": A.combine_bindings(
                [marker["binding"] for marker in markers]
            ),
            "head_object": store.put_content(head, "serve/places-v1/head", ".plhd"),
            "encode_evidence": encode,
            "verify_evidence": verify,
        }


def _head_merge_stage(
    connection: Any, inputs: list[Path], output: Path, result_cap: int
) -> None:
    """One associative top-`result_cap`-per-token merge over a bounded fan-in.

    `top_n(A ∪ B) = top_n(top_n(A) ∪ top_n(B))`, so folding candidates through
    bounded stages produces the same rows as one global pass, order independent.
    """
    connection.execute(
        f"COPY (SELECT * FROM read_parquet([{_sql_paths(inputs)}]) QUALIFY "
        f"row_number() OVER (PARTITION BY token ORDER BY confidence_rank DESC, "
        f"feature_id, source_object_index, source_row_group, source_row_index)"
        f"<={result_cap} ORDER BY {HEAD_ORDER}) TO '{output}' (FORMAT PARQUET, "
        f"COMPRESSION ZSTD, COMPRESSION_LEVEL 6, PARQUET_VERSION V2, PRESERVE_ORDER true)"
    )


# Dual-lane additive head-entry digest domains, identical to the Rust
# `places-serving-encode-v1` / verifier. Summing SHA-256(domain || u64_be(len) ||
# entry) mod 2^256 is commutative/associative, so the digest is
# partition-independent.
HEAD_DIGEST_DOMAIN_A = b"overture-places-head-shard-v1\0"
HEAD_DIGEST_DOMAIN_B = b"overture-places-head-shard-v1\x01"


def _encode_head_entry(row: dict[str, Any]) -> bytes:
    """Re-encode one head serving entry byte-for-byte as the Rust encoder does.

    This is a deliberately independent second implementation of the head-entry
    wire format. It never sees the shard bytes, so the digest it produces over
    the merged head is an independent reduce-side binding the sharded verifier
    reconciles the shard bytes against.
    """
    import struct

    def put_text(buffer: bytearray, value: str) -> None:
        raw = value.encode("utf-8")
        if len(raw) > 0xFFFF:
            raise ValueError("head serving text exceeds u16")
        buffer.extend(len(raw).to_bytes(2, "little"))
        buffer.extend(raw)

    entry = bytearray()
    put_text(entry, row["token"])
    entry.append(row["field_mask"])
    entry.append(row["confidence_rank"])
    feature_id = bytes(row["feature_id"])
    if len(feature_id) != 16:
        raise ValueError("head feature id is not 16 bytes")
    entry.extend(feature_id)
    entry.extend(struct.pack("<d", row["longitude"]))
    entry.extend(struct.pack("<d", row["latitude"]))
    entry.extend(struct.pack("<I", row["source_object_index"]))
    entry.extend(struct.pack("<I", row["source_row_group"]))
    entry.extend(struct.pack("<Q", row["source_row_index"]))
    for name in ("primary_name", "brand_name", "category", "locality", "region", "country"):
        put_text(entry, row[name] if row[name] is not None else "")
    return bytes(entry)


def _independent_merged_head_binding(merged_path: Path) -> dict[str, Any]:
    """Independent dual-lane digest + counts over the pre-shard merged head."""
    import pyarrow.parquet as pq

    sum_a = 0
    sum_b = 0
    records = 0
    tokens: set[str] = set()
    columns = [
        "token", "field_mask", "confidence_rank", "feature_id", "longitude",
        "latitude", "source_object_index", "source_row_group", "source_row_index",
        "primary_name", "brand_name", "category", "locality", "region", "country",
    ]
    parquet = pq.ParquetFile(merged_path)
    for batch in parquet.iter_batches(batch_size=MAX_IPC_BATCH_ROWS, columns=columns):
        for row in batch.to_pylist():
            entry = _encode_head_entry(row)
            prefix = len(entry).to_bytes(8, "big")
            sum_a = (
                sum_a
                + int.from_bytes(
                    hashlib.sha256(HEAD_DIGEST_DOMAIN_A + prefix + entry).digest(), "big"
                )
            ) % A.UINT256
            sum_b = (
                sum_b
                + int.from_bytes(
                    hashlib.sha256(HEAD_DIGEST_DOMAIN_B + prefix + entry).digest(), "big"
                )
            ) % A.UINT256
            tokens.add(row["token"])
            records += 1
    return {
        "records": records,
        "index_entries": len(tokens),
        "head_sum_a": f"{sum_a:064x}",
        "head_sum_b": f"{sum_b:064x}",
    }


def _tree_merge_head_candidates(
    connection: Any,
    candidate_paths: list[Path],
    workspace: Path,
    result_cap: int,
    fan_in: int,
) -> Path:
    """Reduce per-task head candidates to one merged parquet via log-depth stages."""
    if fan_in < 2:
        raise ValueError("head tree-merge fan-in must be at least 2")
    stage = 0
    current = list(candidate_paths)
    while len(current) > 1:
        outputs: list[Path] = []
        for group in range(0, len(current), fan_in):
            chunk = current[group : group + fan_in]
            merged = workspace / f"merge-s{stage}-g{group // fan_in:04d}.parquet"
            _head_merge_stage(connection, chunk, merged, result_cap)
            outputs.append(merged)
        current = outputs
        stage += 1
    if not current:
        raise ValueError("head tree-merge received no candidates")
    return current[0]


def build_sharded_global_head_from_markers(
    *,
    markers: list[dict[str, Any]],
    store: A.LocalObjectStore,
    scratch_root: Path,
    encoder_binary: Path,
    verifier_binary: Path,
    limits: Limits,
    shard_bits: int = DEFAULT_HEAD_SHARD_BITS,
) -> dict[str, Any]:
    """Tree-merge bounded per-task candidates into a hash-sharded global head.

    Per-task head candidates are folded associatively to one merged head, then
    partitioned into `1 << shard_bits` shards by the top bits of each token's
    index hash (the same hash the encoder/Worker use). Each non-empty shard is
    encoded as an independent PLHD head artifact (per-shard entry counts stay far
    under MAX_INDEX_ENTRIES, which remains a fail-closed guard per shard) and the
    whole set is bound by a manifest the sharded verifier reconciles.
    """
    import duckdb

    if not 1 <= shard_bits <= 24:
        raise ValueError("head shard bits out of range")
    if not markers or len(markers) > limits.max_fan_in_tasks:
        raise ValueError("Places head task fan-in exceeds cap")
    task_ids = [marker["task_id"] for marker in markers]
    if len(task_ids) != len(set(task_ids)):
        raise ValueError("Places head contains duplicate map tasks")
    candidates = [marker["head_candidates"] for marker in markers]
    if any(item["result_cap"] != limits.head_result_cap for item in candidates):
        raise ValueError("Places task head cap differs")
    input_rows = sum(item["records"] for item in candidates)
    if input_rows > limits.max_head_candidate_rows:
        raise ValueError("Places merged head candidates exceed row cap")
    shard_count = 1 << shard_bits
    candidate_paths = [store.path(item["object"]["key"]) for item in candidates]
    scratch_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="places-head-sharded-", dir=scratch_root
    ) as name:
        workspace = Path(name)
        connection = duckdb.connect(str(workspace / "head.duckdb"))
        connection.execute(f"SET memory_limit='{limits.duckdb_memory_limit}'")
        connection.execute(f"SET threads={limits.duckdb_threads}")
        connection.execute(f"SET temp_directory='{workspace}'")
        connection.create_function(
            "head_shard",
            lambda token: head_shard_of(token, shard_bits),
            ["VARCHAR"],
            "BIGINT",
        )
        merged = _tree_merge_head_candidates(
            connection,
            candidate_paths,
            workspace,
            limits.head_result_cap,
            limits.max_fan_in_tasks,
        )
        total_records, total_index_entries = connection.execute(
            f"SELECT count(*), count(DISTINCT token) FROM read_parquet('{merged}')"
        ).fetchone()
        # Independent reduce-side binding: digest the merged head with the
        # standalone Python re-encoder before sharding. The sharded verifier
        # reconciles the shard bytes against this, so a shard encoder that
        # consistently drops a token cannot pass (disclosed MAJOR closure).
        merged_head_binding = _independent_merged_head_binding(merged)
        if (
            merged_head_binding["records"] != total_records
            or merged_head_binding["index_entries"] != total_index_entries
        ):
            raise ValueError(
                "Places independent merged-head binding disagrees with the merged head"
            )
        shard_dir = workspace / "shards"
        # PARTITION_BY encodes the shard in the path and omits it from the data
        # files, so each shard parquet carries only the serving columns.
        connection.execute(
            f"COPY (SELECT *, head_shard(token) AS __shard FROM read_parquet('{merged}')) "
            f"TO '{shard_dir}' (FORMAT PARQUET, PARTITION_BY (__shard), COMPRESSION ZSTD, "
            f"COMPRESSION_LEVEL 6, PARQUET_VERSION V2)"
        )
        shard_entries: list[dict[str, Any]] = []
        sum_a = 0
        sum_b = 0
        summed_records = 0
        summed_index_entries = 0
        for shard_path in sorted(shard_dir.glob("__shard=*")):
            shard_id = int(shard_path.name.split("=", 1)[1])
            if not 0 <= shard_id < shard_count:
                raise ValueError("head shard id out of range")
            files = sorted(shard_path.glob("*.parquet"))
            if not files:
                continue
            ordered = workspace / f"shard-{shard_id:06d}.arrow"
            A.write_arrow_query(
                connection,
                f"SELECT * FROM read_parquet([{_sql_paths(files)}]) ORDER BY {HEAD_ORDER}",
                ordered,
                65_536,
            )
            artifact = workspace / f"shard-{shard_id:06d}.plhd"
            sidecar = workspace / f"shard-{shard_id:06d}.digest.json"
            A.run_bounded(
                [
                    str(encoder_binary),
                    "--input",
                    str(ordered),
                    "--output",
                    str(artifact),
                    "--mode",
                    "head",
                    "--digest-out",
                    str(sidecar),
                ],
                scratch_roots=[workspace],
                limits=A.Limits(
                    max_rss_bytes=limits.max_rss_bytes,
                    max_scratch_bytes=limits.max_scratch_bytes,
                    wall_seconds=limits.wall_seconds,
                ),
            )
            if artifact.stat().st_size > limits.max_output_bytes:
                raise ValueError("Places head shard exceeds output cap")
            digest = json.loads(sidecar.read_text())
            stored = store.put_content(artifact, "serve/places-v1/head", ".plhd")
            shard_entries.append(
                {
                    "shard_id": shard_id,
                    "path": str(store.path(stored["key"])),
                    "key": stored["key"],
                    "bytes": artifact.stat().st_size,
                    "records": digest["records"],
                    "index_entries": digest["index_entries"],
                    "head_sum_a": digest["head_sum_a"],
                    "head_sum_b": digest["head_sum_b"],
                }
            )
            sum_a = (sum_a + int(digest["head_sum_a"], 16)) % A.UINT256
            sum_b = (sum_b + int(digest["head_sum_b"], 16)) % A.UINT256
            summed_records += digest["records"]
            summed_index_entries += digest["index_entries"]
            ordered.unlink()
        connection.close()
        if summed_records != total_records or summed_index_entries != total_index_entries:
            raise ValueError("Places head sharding dropped or duplicated rows")
        # The Rust per-shard digest sums must also match the independent binding;
        # the sharded verifier re-checks this from bytes, we assert it early here.
        if (
            f"{sum_a:064x}" != merged_head_binding["head_sum_a"]
            or f"{sum_b:064x}" != merged_head_binding["head_sum_b"]
        ):
            raise ValueError(
                "Places per-shard head digest sums differ from the independent binding"
            )
        input_binding = A.combine_bindings([marker["binding"] for marker in markers])
        manifest = {
            "schema": "overture-places-global-head-sharded-v2",
            "shard_count": shard_count,
            "shard_bits": shard_bits,
            "result_cap": limits.head_result_cap,
            "task_ids": sorted(task_ids),
            "input_candidate_rows": input_rows,
            "total_records": total_records,
            "total_index_entries": total_index_entries,
            "head_sum_a": f"{sum_a:064x}",
            "head_sum_b": f"{sum_b:064x}",
            "merged_head_binding": merged_head_binding,
            "input_binding": input_binding,
            "shards": [
                {key: entry[key] for key in entry if key != "key"}
                for entry in shard_entries
            ],
        }
        manifest_path = workspace / "head-manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
        verify = A.run_bounded(
            [
                str(verifier_binary),
                "--mode",
                "head-sharded",
                "--manifest",
                str(manifest_path),
            ],
            scratch_roots=[workspace],
            limits=A.Limits(
                max_rss_bytes=limits.max_rss_bytes,
                max_scratch_bytes=limits.max_scratch_bytes,
                wall_seconds=limits.wall_seconds,
            ),
        )
        manifest_object = store.put_content(
            manifest_path, "serve/places-v1/head-manifest", ".json"
        )
        return {
            "schema": "overture-places-global-head-sharded-v2",
            "shard_count": shard_count,
            "shard_bits": shard_bits,
            "result_cap": limits.head_result_cap,
            "task_ids": sorted(task_ids),
            "input_candidate_rows": input_rows,
            "total_records": total_records,
            "total_index_entries": total_index_entries,
            "populated_shards": len(shard_entries),
            "head_sum_a": f"{sum_a:064x}",
            "head_sum_b": f"{sum_b:064x}",
            "merged_head_binding": merged_head_binding,
            "input_binding": input_binding,
            "manifest_object": manifest_object,
            "shard_objects": [
                {"shard_id": entry["shard_id"], "key": entry["key"]}
                for entry in shard_entries
            ],
            "verify_evidence": verify,
        }
