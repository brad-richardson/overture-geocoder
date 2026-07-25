#!/usr/bin/env python3
"""Complete local Places construction-v1 map/genesis/reduce/head slice."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import time
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
REDUCE_RANGE_SCHEMA = "overture-places-bucket-range-reduce-v1"
TOTAL_ORDER = (
    "execution_group, partition_cell, token, confidence_rank DESC, feature_id, "
    "source_object_index, source_row_group, source_row_index"
)
HEAD_ORDER = (
    "token, confidence_rank DESC, feature_id, source_object_index, "
    "source_row_group, source_row_index"
)
# The serving-candidate ranking, used in EXACTLY two places: the map-side
# combiner and the reducer's serving QUALIFY. It must be one constant, not two
# literals. The map combiner is only exact because it ranks rows the same way
# the reducer does -- if the two ever disagreed, every map task would retain the
# wrong candidates and the reducer would serve them, with no row lost, no
# binding violated, and no test failing. A shared constant makes that class of
# bug impossible rather than merely unobserved.
SERVING_ORDER = (
    "confidence_rank DESC, feature_id, source_object_index, "
    "source_row_group, source_row_index"
)
SERVING_PARTITION = "PARTITION BY partition_cell, token"
# The combiner deletes rows outside the top-N of each (partition_cell, token)
# group. That is exact ONLY while a group is never split across two reduce
# partitions -- otherwise a row outside the global top-N could be inside a
# sub-partition's top-N, and the combiner would already have deleted it.
# Subdivision is by token hash, so every row of a token shares a prefix at every
# depth and the group is indivisible. reduce_partition asserts this.
SERVING_GROUP_SAFE_OWNERSHIP = "token-sha256-nibble-prefix-v1"

# --- shuffle -------------------------------------------------------------
# Map tasks are row-group ranges of the SOURCE, so without a shuffle every
# partition_cell is scattered across every task and any consumer of one cell has
# to reach into all of them. That scatter -- not size -- is why the whole store
# had to travel between phases.
#
# So map emits its output keyed by a bucket of the cell, exactly as the ID index
# stages by UUID prefix (build_id_index.py). Two properties matter:
#
#   a cell never splits across buckets, so one consumer holds a cell's COMPLETE
#   data and every per-cell decision (top-N, subdivision, cap checks) is local;
#
#   buckets are hash-uniform, so per-consumer input is bounded by construction
#   at total/SHUFFLE_BUCKETS rather than by how the data happens to be shaped.
#
# The bucket is a Knuth multiplicative hash of `partition_key` -- the (y<<8)|x
# grid index the transform already emits -- so it needs no new column and no
# DuckDB hash function whose value could drift across versions.
SHUFFLE_BUCKET_BITS = 8
SHUFFLE_BUCKETS = 1 << SHUFFLE_BUCKET_BITS
SHUFFLE_MULTIPLIER = 2_654_435_761  # Knuth, floor(2^32 / phi), odd


def shuffle_bucket_sql(bits: int = SHUFFLE_BUCKET_BITS) -> str:
    return (
        f"((((partition_key::UBIGINT * {SHUFFLE_MULTIPLIER}) % 4294967296) "
        f">> {32 - bits}))::UINTEGER"
    )


def shuffle_bucket(partition_key: int, bits: int = SHUFFLE_BUCKET_BITS) -> int:
    """Python mirror of shuffle_bucket_sql; the two must never disagree.

    Takes the HIGH bits of the multiplicative hash. Taking the low bits instead
    (``% buckets``) silently degenerates: partition_key is ``(y << 8) | x``, so
    the low 8 bits of the product depend only on x, and every cell in a
    longitude column would land in one bucket -- a pole-to-pole meridian strip
    per consumer. Cell counts stay perfectly even, so only a data-weighted test
    catches it.
    """
    return ((partition_key * SHUFFLE_MULTIPLIER) % 4294967296) >> (32 - bits)


def cell_partition_key(cell: str) -> int:
    """(y<<8)|x for a `{y:02x}{x:02x}` partition cell, matching route()."""
    if len(cell) != 4:
        raise ValueError(f"Places partition cell is malformed: {cell!r}")
    return (int(cell[:2], 16) << 8) | int(cell[2:], 16)


def partition_shuffle_bucket(
    partition: dict[str, Any], bits: int = SHUFFLE_BUCKET_BITS
) -> int:
    """The bucket a partition's cell was shuffled into, derived not trusted.

    The plan records `shuffle_bucket`, but a reduce job that owns a bucket RANGE
    must not take that record on faith: a plan written at different
    `shuffle_bucket_bits` than the reducer reads would silently move cells
    between ranges, dropping some and duplicating others. Derive it and fail
    closed when the record disagrees.
    """
    derived = shuffle_bucket(cell_partition_key(partition["partition_cell"]), bits)
    recorded = partition.get("shuffle_bucket")
    if recorded is not None and int(recorded) != derived:
        raise ValueError(
            "Places partition records shuffle bucket "
            f"{recorded} but its cell hashes to {derived} at {bits} bits"
        )
    return derived


def validate_bucket_range(
    bucket_start: int, bucket_end: int, bits: int = SHUFFLE_BUCKET_BITS
) -> tuple[int, int]:
    """Bounds-check an INCLUSIVE bucket range, as build_id_index.py does.

    Inclusive on both ends deliberately: it mirrors
    `build_id_index.py --prefix-start/--prefix-end`, so the two range-owning
    consumers in this repo share one convention rather than each having its own.
    """
    buckets = 1 << bits
    start, end = int(bucket_start), int(bucket_end)
    if not 0 <= start <= end < buckets:
        raise ValueError(
            f"Places shuffle bucket range [{start},{end}] is outside 0..{buckets - 1}"
        )
    return start, end


def bucket_range_partitions(
    plan: dict[str, Any],
    *,
    bucket_start: int,
    bucket_end: int,
    bits: int = SHUFFLE_BUCKET_BITS,
) -> list[tuple[int, dict[str, Any]]]:
    """Every partition of `plan` whose cell falls in the INCLUSIVE bucket range.

    Ownership is exact by construction: a cell hashes to exactly one bucket and a
    bucket belongs to exactly one range, so for ANY partition of the bucket space
    into ranges each partition is emitted by exactly one range and the union of
    ranges emits all of them.
    """
    start, end = validate_bucket_range(bucket_start, bucket_end, bits)
    return [
        (index, partition)
        for index, partition in enumerate(plan["partitions"])
        if start <= partition_shuffle_bucket(partition, bits) <= end
    ]


def bucket_range_fragments(
    markers: list[dict[str, Any]],
    *,
    bucket_start: int,
    bucket_end: int,
    bits: int = SHUFFLE_BUCKET_BITS,
) -> list[dict[str, Any]]:
    """The map fragments a bucket range must read, from the map markers.

    `pack_id` IS the shuffle bucket, so the fragment set of a range is a simple
    filter over the markers rather than something re-derived per partition from
    routing summaries. Only term packs are considered: a marker also carries
    head-candidate and per-place artifacts, which are separate keys and are not
    fragments of the term shuffle.
    """
    start, end = validate_bucket_range(bucket_start, bucket_end, bits)
    fragments: list[tuple[int, str, dict[str, Any]]] = []
    for marker in markers:
        for pack in marker["packs"]:
            bucket = pack.get("shuffle_bucket")
            if bucket is None:
                raise ValueError("Places map marker predates the map-side shuffle")
            if int(bucket) != int(pack["pack_id"]):
                raise ValueError("Places pack id differs from its shuffle bucket")
            if start <= int(bucket) <= end:
                fragments.append((int(bucket), marker["task_id"], pack))
    # Bucket-major so a range reads its fragments in shuffle order, and the
    # per-fragment scan is a contiguous run of the packed key space.
    fragments.sort(key=lambda item: (item[0], item[1]))
    packs = [pack for _, _, pack in fragments]
    # "Each fragment once" is a claim about OBJECTS, not about list entries, so
    # the object keys must be unique. A duplicate here -- two markers naming the
    # same pack, or one marker listing it twice -- would double-count every row
    # it holds, and because bindings are additive sums the inflation would be
    # invisible until a reconciliation far downstream. Fail closed instead.
    keys = [pack["object"]["key"] for pack in packs]
    if len(keys) != len(set(keys)):
        raise ValueError("Places bucket range names the same map fragment twice")
    return packs


COMBINER_SCHEMA = "overture-places-map-combiner-v1"
# The per-place positions artifact: one row per ADMITTED place RECORD, bucketed
# by the same shuffle as the term packs. Named distinctly from the term packs
# ("positions" vs "packs") because the two ride the same shuffle and a consumer
# must never confuse them: term rows are ~7.19 per record and post-combiner,
# these are exactly one per record and pre-combiner.
POSITIONS_SCHEMA = "overture-places-map-positions-v1"
POSITIONS_DIRECTORY_SCHEMA = "overture-places-map-positions-directory-v1"
# One row per admitted place RECORD, keyed by feature_id plus the source locator
# -- the same identity the serving path uses. Not keyed by feature_id alone: the
# frozen evidence spec requires every copy of a repeated id to survive as a
# distinct candidate keyed by its provenance (test_places_duplicate_uuid_gate).
#
# Self-sufficient by decision, not by drift: a reverse hit has to render a name
# and a category, the ID index returns neither, and `/v2/features/:gers_id` is
# being removed -- so a positions-only row could not answer a reverse query.
POSITIONS_COLUMNS = (
    "feature_id",
    "partition_cell",
    "longitude",
    "latitude",
    "primary_name",
    "brand_name",
    "category",
    "locality",
    "region",
    "country",
    "confidence_rank",
    "source_object_index",
    "source_row_group",
    "source_row_index",
)
POSITIONS_COLUMNS_SQL = ", ".join(POSITIONS_COLUMNS)
# Total within a pack, because the locator is unique per source record.
POSITIONS_ORDER = (
    "partition_cell, feature_id, source_object_index, source_row_group, "
    "source_row_index"
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
    # PRODUCTION partition caps, kept equal to `construction_v1_hosted
    # .HOSTED_LIMITS["places"]` (asserted by
    # tests/test_construction_v1_preflight.py). They were raised from
    # 1,000,000 / 256 MiB / 250,000 after the 2026-07-22.0 growth test; the
    # measured justification per cap lives at the HOSTED_LIMITS site. Anything
    # that plans on these defaults -- every caller that is not the hosted CLI --
    # now plans at the caps the planet build actually uses.
    #
    # The one deliberate exception is `rehearse_places_construction_v1.py`: it
    # produces evidence under the FROZEN places evidence spec v2, whose
    # relaxation policy is "none", so it pins the three caps that spec declares
    # instead of these. See docs/plans/2026-07-24-construction-v1-follow-ups.md.
    partition_term_rows: int = 2_000_000
    partition_estimated_bytes: int = 512 * 1024**2
    partition_distinct_tokens: int = 400_000
    adaptive_subdivision_depth: int = 8
    maximum_serving_candidates: int = 256
    # Number of shuffle buckets map keys its output by. Raising it lowers
    # per-consumer input proportionally at the cost of more objects; it is
    # the single knob that bounds reduce input independently of data shape.
    shuffle_bucket_bits: int = SHUFFLE_BUCKET_BITS
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
                    self.shuffle_bucket_bits,
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
    # An admitted marker must record the combiner, and its additivity must still
    # reconstruct the transform. Without this a pre-combiner marker resumes
    # silently and one run mixes combined and uncombined tasks.
    combiner = marker.get("combiner")
    if not isinstance(combiner, dict) or combiner.get("schema") != COMBINER_SCHEMA:
        raise ValueError("Places marker is missing its combiner record")
    if combiner.get("retained_rows") != marker["binding"]["records"]:
        raise ValueError("Places combiner retained rows differ from the marker binding")
    if (
        A.combine_bindings([marker["binding"], combiner["discarded"]])["records"]
        != marker["transform"]["emitted_term_rows"]
    ):
        raise ValueError("Places combiner kept+discarded differs from transform")
    validate_positions(marker, store, task_id)
    return marker


def positions_directory(
    parquet_path: Path, *, bucket: int, bits: int
) -> dict[str, Any]:
    """Row-group directory for one positions pack, and its cell invariant.

    The term packs get an exact two-lane binding from the Rust proof binary,
    which reads `semantic_digest_a`/`_b` off each TERM row. A positions row is a
    derived, per-feature row and carries no such digest, and inventing one (say
    `min()` over a feature's term digests) would produce a proof frame that
    looks exact while binding nothing that exists. So a positions pack is bound
    by its content hash (`put_content`) plus this directory: per-row-group record
    counts and per-cell record counts, which is what a consumer needs to skip
    row groups and to check that it read every row it was promised.

    It also enforces the property the shuffle exists for: every cell in the pack
    hashes to the pack's own bucket, so a cell is never split across buckets.
    """
    import pyarrow.parquet as pq

    parquet = pq.ParquetFile(parquet_path)
    row_groups = []
    totals: dict[str, int] = {}
    for index in range(parquet.metadata.num_row_groups):
        cells = parquet.read_row_group(index, columns=["partition_cell"])
        counts: dict[str, int] = {}
        for cell in cells.column("partition_cell").to_pylist():
            if cell is None:
                raise ValueError("Places positions row carries no partition cell")
            counts[cell] = counts.get(cell, 0) + 1
            totals[cell] = totals.get(cell, 0) + 1
        row_groups.append(
            {
                "index": index,
                "records": cells.num_rows,
                "cells": [
                    {"partition_cell": cell, "records": counts[cell]}
                    for cell in sorted(counts)
                ],
            }
        )
    for cell in totals:
        if shuffle_bucket(cell_partition_key(cell), bits) != bucket:
            raise ValueError("Places positions cell landed in the wrong shuffle bucket")
    return {
        "schema": POSITIONS_DIRECTORY_SCHEMA,
        "shuffle_bucket": bucket,
        "records": parquet.metadata.num_rows,
        "row_groups": row_groups,
        "cells": [
            {"partition_cell": cell, "records": totals[cell]} for cell in sorted(totals)
        ],
    }


def emit_positions(
    connection: Any,
    *,
    workspace: Path,
    store: A.LocalObjectStore,
    limits: Limits,
    admitted_features: int,
) -> dict[str, Any]:
    """Emit the per-place-record, cell-keyed positions packs from `terms`.

    One row per ADMITTED PLACE RECORD -- not per distinct place. The unit is the
    source record, keyed by `feature_id` PLUS its source locator
    (`source_object_index`, `source_row_group`, `source_row_index`), which is
    exactly the identity the serving path uses. Overture ids are effectively
    unique, but the frozen evidence spec requires that a repeated id survive as
    several distinct candidates keyed by provenance
    (tests/test_places_duplicate_uuid_gate.py), so collapsing by `feature_id`
    would both violate that contract and abort a planet map job on data the
    contract declares valid. Consumers that want one row per place dedupe by
    their own policy; this artifact does not choose one for them.

    Because the unit is the record, `records == admitted_features` holds exactly
    and no aggregation is involved: the rows are a projection with a `DISTINCT`
    that removes only the per-token fan-out, so no coordinate or name is ever
    synthesized across rows.

    The record is SELF-SUFFICIENT: position plus the fields a reverse result has
    to render (`primary_name`, `brand_name`, `category`, `locality`, `region`,
    `country`, `confidence_rank`). The ID index returns no names and
    `/v2/features/:gers_id` is being removed, so a positions-only row could not
    render a reverse hit at all.

    Two things make the artifact worth its bytes, and both are about WHEN it is
    produced rather than what it contains.

    It must be derived BEFORE the combiner. The combiner keeps only the top
    `maximum_serving_candidates` rows per (partition_cell, token), so a place
    whose tokens are all generic and all sit in saturated groups can vanish from
    the term set entirely. Harmless for forward search, silently missing from
    anything that must ENUMERATE places -- a spatial reverse index above all.
    Deriving positions from the combined set would lose exactly those places.

    And it must be produced HERE rather than added later: term rows and head
    candidates are both the wrong shape, so a reverse index bolted on afterwards
    costs a full planet map re-run. Emitting it now makes that work purely
    additive (docs/plans/construction-v1-state.md).

    Packs are ordered by (partition_cell, feature_id, locator) within a bucket
    and the buckets are written in ascending order, which is the global
    (shuffle_bucket, partition_cell, feature_id, locator) order -- total, because
    the locator is unique per source record.
    """
    import pyarrow.parquet as pq

    bits = limits.shuffle_bucket_bits
    # Bucketed exactly like the term packs: one tagged copy, then one file per
    # present bucket. `COPY ... PARTITION_BY` cannot be used because DuckDB does
    # not preserve row order within a partition, and these packs are sorted.
    tagged = workspace / "positions-tagged.parquet"
    connection.execute(
        f"COPY (SELECT DISTINCT {POSITIONS_COLUMNS_SQL}, "
        f"  {shuffle_bucket_sql(bits)} pack_id FROM terms) "
        f"TO '{tagged}' (FORMAT PARQUET, COMPRESSION ZSTD, COMPRESSION_LEVEL 6, "
        f"ROW_GROUP_SIZE {limits.parquet_row_group_rows}, PARQUET_VERSION V2)"
    )
    records = pq.ParquetFile(tagged).metadata.num_rows
    # EXACTLY one row per admitted record. The DISTINCT removes only the
    # per-token fan-out, so the count can differ from `admitted_features` in one
    # way: two admitted records sharing a full identity INCLUDING their source
    # locator, which the projection cannot produce and the serving path could not
    # tell apart either. Fail closed rather than silently drop a record from
    # every consumer of this artifact.
    if records != admitted_features:
        raise ValueError("Places positions differ from the admitted record count")
    source = f"read_parquet('{tagged}')"
    present = [
        int(row[0])
        for row in connection.execute(
            f"SELECT DISTINCT pack_id FROM {source} ORDER BY pack_id"
        ).fetchall()
    ]
    packs = []
    for pack_id in present:
        pack = workspace / f"positions-{pack_id:06d}.parquet"
        connection.execute(
            f"COPY (SELECT * EXCLUDE(pack_id) FROM {source} WHERE pack_id={pack_id} "
            f"ORDER BY {POSITIONS_ORDER}) "
            f"TO '{pack}' (FORMAT PARQUET, COMPRESSION ZSTD, COMPRESSION_LEVEL 6, "
            f"ROW_GROUP_SIZE {limits.parquet_row_group_rows}, PARQUET_VERSION V2, "
            "PRESERVE_ORDER true)"
        )
        if pack.stat().st_size > limits.max_output_bytes:
            raise ValueError("Places positions pack exceeds output cap")
        directory_value = positions_directory(pack, bucket=pack_id, bits=bits)
        directory_path = workspace / f"positions-{pack_id:06d}.directory.json"
        directory_path.write_text(json.dumps(directory_value, sort_keys=True) + "\n")
        packs.append(
            {
                "pack_id": pack_id,
                "shuffle_bucket": pack_id,
                "records": directory_value["records"],
                "object": store.put_content(
                    pack, "map/places-v1/positions", ".parquet"
                ),
                "directory_object": store.put_content(
                    directory_path, "map/places-v1/position-directories", ".json"
                ),
                "directory": directory_value,
            }
        )
        pack.unlink(missing_ok=True)
        directory_path.unlink(missing_ok=True)
    tagged.unlink(missing_ok=True)
    if sum(pack["records"] for pack in packs) != records:
        raise ValueError("Places positions packs do not reconstruct the record count")
    return {
        "schema": POSITIONS_SCHEMA,
        "records": records,
        "admitted_features": admitted_features,
        "shuffle_bucket_bits": bits,
        "packs": packs,
    }


def validate_positions(
    marker: dict[str, Any], store: A.LocalObjectStore, task_id: str | None = None
) -> None:
    """A resumed marker must carry the positions artifact, intact.

    Without this an admitted marker written before the artifact existed resumes
    silently and one run mixes tasks that have positions with tasks that do not
    -- the same failure mode the combiner check above closes. Resuming a run that
    predates this artifact therefore aborts its map jobs BY DESIGN; the error
    says what to do about it.
    """
    positions = marker.get("positions")
    if not isinstance(positions, dict) or positions.get("schema") != POSITIONS_SCHEMA:
        hint = marker_key(task_id) if task_id else marker_key(marker["task_id"])
        raise ValueError(
            "Places marker is missing its per-place positions artifact "
            f"(pre-positions marker; delete {hint} to re-map this task, or "
            "resume from a post-positions run)"
        )
    packs = positions.get("packs")
    if not packs:
        raise ValueError("Places positions artifact records no packs")
    for pack in packs:
        for identity in (pack["object"], pack["directory_object"]):
            path = store.path(identity["key"])
            if (
                not path.is_file()
                or path.stat().st_size != identity["bytes"]
                or A.sha256_file(path) != identity["sha256"]
            ):
                raise ValueError("Places immutable positions object is missing or changed")
    if sum(pack["records"] for pack in packs) != positions["records"]:
        raise ValueError("Places positions packs do not reconstruct the record count")
    if positions["records"] != marker["transform"]["admitted_features"]:
        raise ValueError("Places positions differ from the admitted record count")


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
    import pyarrow as pa
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
        # Fail closed on ANY invalid_source_locator: a correctly report-derived
        # source-limits bound never rejects a real projected locator, so a
        # non-zero count means the limits or projected row-group/row indices are
        # inconsistent (the silent wrong-bytes class the old row_groups:1 defect
        # exposed: the transform exits 0 while dropping rows).
        if transform.get("rejections_by_precedence", {}).get("invalid_source_locator", 0):
            raise ValueError(
                "places transform rejected projected locators as invalid_source_locator; "
                "source-limits and projected row-group/row indices are inconsistent"
            )
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

        positions = emit_positions(
            connection,
            workspace=workspace,
            store=store,
            limits=limits,
            admitted_features=transform["admitted_features"],
        )
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
        # Combiner. reduce_partition only ever serves the top
        # `maximum_serving_candidates` rows per (partition_cell, token); every
        # row below that rank is carried across the whole pipeline and then
        # discarded. Applying the same top-N here is EXACT, not lossy: top-N
        # under a TOTAL order is decomposable, so a per-task top-N followed by
        # the reducer's global top-N over the union yields the same rows as a
        # single global top-N. (Any row in the global top-N has at most N-1 rows
        # ahead of it globally, hence at most N-1 within its own task.)
        #
        # This runs AFTER head_candidates so the head phase is provably
        # untouched -- head_result_cap (10) <= maximum_serving_candidates (256)
        # would make it safe either way, but ordering it here removes the
        # coupling entirely.
        #
        # Measured on release 2026-07-22.0: 533,964,455 -> 286,494,538 term rows
        # (46.3% removed), and the largest indivisible (cell, token) group falls
        # from 742,392 rows to 1,078 -- which is what dissolves the hard floor
        # that no subdivision depth could lower.
        combine_rank = (
            f"row_number() OVER ({SERVING_PARTITION} ORDER BY {SERVING_ORDER})"
        )

        def binding_of(sql: str) -> dict[str, Any]:
            reader = connection.execute(sql).fetch_record_batch(MAX_IPC_BATCH_ROWS)
            total = A.zero_binding()
            for batch in reader:
                total = A.combine_bindings(
                    [total, binding_for_table(pa.Table.from_batches([batch]))]
                )
            return total

        # Deliberately NO materialized `ranked` table. Ranking into a second
        # table and then splitting it holds `terms` + `ranked` + the new table at
        # once -- measured at ~3x the term-table size -- which breaks the staged
        # deletion discipline the pack export below depends on. Streaming the
        # discarded binding straight off `terms` costs a second sort and peaks
        # at ~1.5x instead.
        #
        # The discarded set is proven, not assumed: its binding plus the packs'
        # binding must reconstruct the transform's binding exactly. That is the
        # same selected/discarded additivity the reducer already enforces per
        # row group, so nothing is taken on trust because rows were dropped.
        discarded_binding = binding_of(
            f"SELECT * FROM terms QUALIFY {combine_rank} > "
            f"{limits.maximum_serving_candidates}"
        )
        connection.execute(
            f"CREATE TABLE combined AS SELECT * FROM terms QUALIFY {combine_rank} "
            f"<= {limits.maximum_serving_candidates}"
        )
        connection.execute("DROP TABLE terms")
        connection.execute("CHECKPOINT")
        connection.execute("ALTER TABLE combined RENAME TO terms")
        connection.execute("CHECKPOINT")
        combined_rows = connection.execute("SELECT count(*) FROM terms").fetchone()[0]
        combiner = {
            "schema": COMBINER_SCHEMA,
            "serving_candidate_cap": limits.maximum_serving_candidates,
            "input_rows": transform["emitted_term_rows"],
            "retained_rows": combined_rows,
            "discarded": discarded_binding,
        }
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
            # pack_id is the SHUFFLE BUCKET, not a row counter. This is the
            # whole change: a pack now holds every row this task has for a set
            # of cells, and holds nothing for any other cell, so a consumer of
            # those cells reads this one file instead of scanning for its rows.
            connection.execute(
                f"COPY (SELECT *, {shuffle_bucket_sql(limits.shuffle_bucket_bits)} "
                f"pack_id FROM terms) "
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
            # Only buckets this task actually produced. A sparse task (few
            # cells) writes few fragments; nothing emits empty objects.
            present = [
                int(row[0])
                for row in connection.execute(
                    f"SELECT DISTINCT pack_id FROM {packed_source} ORDER BY pack_id"
                ).fetchall()
            ]
            for pack_id in present:
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
                        "shuffle_bucket": pack_id,
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
        if binding["records"] != combined_rows:
            raise ValueError("Places map binding differs from the combined term set")
        # The packs no longer carry every transformed row, so the map no longer
        # asserts packs == transform. It asserts the strictly stronger
        # additivity: what was kept plus what was proven discarded reconstructs
        # the transform's binding exactly, over both digest lanes.
        reconstructed = A.combine_bindings([binding, discarded_binding])
        if (
            reconstructed["records"] != transform["emitted_term_rows"]
            or reconstructed["semantic_sum_a"] != transform["semantic_sum_a"]
            or reconstructed["semantic_sum_b"] != transform["semantic_sum_b"]
        ):
            raise ValueError("Places map kept+discarded differs from transform")
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
            "combiner": combiner,
            "positions": positions,
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
    # Pack bodies are fetched in BATCHES and released after each batch, never all
    # at once. The eager `[store.path(k) for k in packs]` this replaced defeated
    # `max_fan_in_packs` entirely: with the store in R2 staging every pack was
    # hydrated onto the plan runner before DuckDB read the first one, which at
    # planet scale is the whole ~34 GB term store on the very job that killed run
    # 30113308268. `release` is present only on the staged store, so a local-only
    # store is untouched -- evicting there would delete the store itself.
    release = getattr(store, "release", None)
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
        for start in range(0, len(packs), limits.max_fan_in_packs):
            batch = packs[start : start + limits.max_fan_in_packs]
            batch_paths = [store.path(pack["object"]["key"]) for pack in batch]
            statement = (
                "CREATE TABLE planning AS" if start == 0 else "INSERT INTO planning"
            )
            connection.execute(
                f"{statement} SELECT partition_cell, token, token_hash, "
                f"({byte_expression})::UBIGINT estimated_bytes FROM "
                f"read_parquet([{_sql_paths(batch_paths)}])"
            )
            # The batch is now inside DuckDB's own bounded table; the pack bodies
            # are not needed again until the binding pass below, which re-fetches
            # one at a time.
            if release is not None:
                for pack in batch:
                    release(pack["object"]["key"])
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
                        # The shuffle bucket map wrote this cell's rows into.
                        # Recorded on the partition so the plan can be ORDERED by
                        # it below, which is what turns the existing contiguous
                        # batching into bucket-clustered batching.
                        "shuffle_bucket": shuffle_bucket(
                            cell_partition_key(cell), limits.shuffle_bucket_bits
                        ),
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
        # Second pass over the same packs, ONE at a time: this reads four columns
        # per pack and accumulates per-partition bindings, so nothing needs a
        # second pack resident. Peak local bytes for the whole plan phase is
        # therefore max(max_fan_in_packs packs, one pack).
        for pack in packs:
            path = store.path(pack["object"]["key"])
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
            del parquet
            if release is not None:
                release(pack["object"]["key"])
        for partition, accumulator in zip(partitions, accumulators, strict=True):
            partition["binding"] = {
                "records": accumulator["records"],
                "semantic_sum_a": f"{accumulator['semantic_sum_a']:064x}",
                "semantic_sum_b": f"{accumulator['semantic_sum_b']:064x}",
            }
            if partition["binding"]["records"] != partition["term_rows"]:
                raise ValueError("Places adaptive partition proof row count differs")
        # Order partitions by SHUFFLE BUCKET, not by cell.
        #
        # Reduce batches are contiguous ranges of partition index
        # (construction_v1_hosted._reduce_batches). Ordered by cell, a 140-
        # partition batch spans ~140 unrelated buckets and therefore needs a
        # fragment from every bucket -- which is to say, the whole store. Ordered
        # by bucket, a batch covers a contiguous BUCKET range, so it needs only
        # the fragments in that range. That is the property R2 staging then turns
        # into "fetch only your own shards"; without it, staging would still have
        # every reducer pulling everything.
        #
        # (cell, depth, prefix) stays as the tiebreak so the order is total and
        # the plan remains byte-reproducible.
        partitions.sort(
            key=lambda item: (
                item["shuffle_bucket"],
                item["partition_cell"],
                item["ownership"]["depth"],
                item["ownership"]["prefix"],
            )
        )
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


# The reducer's per-row partition tag. A bucket-range reduce ingests every
# partition in its range into ONE table and separates them on the way out. The
# alternative -- one table per partition -- multiplies DuckDB catalog and
# checkpoint work by the partition count for nothing, since the rows are
# disjoint by construction: a row belongs to exactly one partition.
PARTITION_INDEX_COLUMN = "__reduce_partition"


def _reduce_connection(workspace: Path, limits: Limits) -> Any:
    import duckdb

    spill = workspace / "spill"
    spill.mkdir(exist_ok=True)
    connection = duckdb.connect(str(workspace / "reduce.duckdb"))
    connection.execute(f"SET memory_limit='{limits.duckdb_memory_limit}'")
    connection.execute(f"SET threads={limits.duckdb_threads}")
    connection.execute(f"SET temp_directory='{spill}'")
    return connection


def _reduce_watchdog(
    workspace: Path, limits: Limits, connection: Any, wall_seconds: float | None = None
):
    """The whole-stage RSS/scratch/wall guard, on the reducer's own terms.

    The encoder and verifier are bounded because they are subprocesses under
    `A.run_bounded`. The Python + pyarrow + DuckDB work between them was not
    bounded by anything at all, which made reduce the one phase whose peak
    nothing observed -- and raising `partition_term_rows` doubled exactly that
    peak. Same caps, same fail-closed semantics, same evidence shape as the two
    `map_task` stages.

    `wall_seconds` is the REMAINING job budget, not a fresh one per stage. A
    bucket-range job runs one ingest and then tens of serving stages; giving each
    the full `limits.wall_seconds` would leave the JOB unbounded in wall time --
    ~66 partitions x 18,000 s against a 330-minute Actions kill that produces no
    evidence at all.
    """
    return A.StageWatchdog(
        [workspace],
        A.Limits(
            max_rss_bytes=limits.max_rss_bytes,
            max_scratch_bytes=limits.max_scratch_bytes,
            wall_seconds=limits.wall_seconds if wall_seconds is None else wall_seconds,
        ),
        connection,
    )


def _remaining_wall(started: float, limits: Limits) -> float:
    """What is left of the JOB's wall budget, failing closed when it is gone."""
    remaining = limits.wall_seconds - (time.monotonic() - started)
    if remaining <= 0:
        raise ValueError(
            "Places reduce job exhausted its wall budget of "
            f"{limits.wall_seconds}s before finishing its partitions"
        )
    return remaining


def _reduce_preconditions(
    plan: dict[str, Any],
    markers: list[dict[str, Any]],
    partitions: list[dict[str, Any]],
    limits: Limits,
) -> None:
    if sorted(marker["task_id"] for marker in markers) != plan["marker_task_ids"]:
        raise ValueError("Places reducer marker set is missing or extra")
    # The map already deleted every row outside the top-N of each
    # (partition_cell, token) group, so the reducer MUST rank with the same N.
    # Reducing at a larger N would silently serve a truncated candidate list --
    # no row lost, no binding violated, no error raised anywhere.
    # `maximum_serving_candidates` is a Limits field and _limits_for lets a
    # contract override it, so the stages can drift across a re-derived contract
    # on a resumed run. Mirrors the existing "Places task head cap differs".
    for marker in markers:
        recorded = marker.get("combiner")
        if not isinstance(recorded, dict):
            raise ValueError("Places reducer marker predates the map combiner")
        if recorded.get("serving_candidate_cap") != limits.maximum_serving_candidates:
            raise ValueError(
                "Places map combine cap differs from the reducer serving cap"
            )
    # And the combiner is exact only while a (partition_cell, token) group is
    # never split across partitions -- true for token-hash subdivision, false
    # for any within-token split dimension.
    for partition in partitions:
        ownership = partition.get("ownership", {}).get(
            "kind", SERVING_GROUP_SAFE_OWNERSHIP
        )
        if ownership != SERVING_GROUP_SAFE_OWNERSHIP:
            raise ValueError(
                "Places partition ownership splits a token group; the map combiner "
                "is not exact under this scheme"
            )


def _reduce_ingest(
    *,
    connection: Any,
    fragments: list[dict[str, Any]],
    partitions: list[tuple[int, dict[str, Any]]],
    store: A.LocalObjectStore,
    require_complete: bool,
) -> dict[str, Any]:
    """Read each fragment ONCE and tag every row with the partition that owns it.

    `partitions` is the (plan index, partition) set this job emits; a row's owner
    is looked up by cell, so one pass over a fragment feeds every partition whose
    cell that fragment holds. The old per-partition reducer walked the whole
    marker set once PER PARTITION and re-derived its row groups each time; a
    fragment holding k of a job's cells was therefore opened k times.
    """
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    positions_of_cell: dict[str, list[int]] = {}
    for position, (_plan_index, partition) in enumerate(partitions):
        positions_of_cell.setdefault(partition["partition_cell"], []).append(position)
    reconciled: list[list[dict[str, Any]]] = [[] for _ in partitions]
    selected_bindings: list[list[dict[str, Any]]] = [[] for _ in partitions]
    initialized = False
    maximum_batch_rows = 0
    fragments_opened = 0
    for pack in fragments:
        # Which row groups of this fragment hold a cell this job owns, and which
        # of its partitions each row group can feed. Row-group skipping is the
        # point of the sorted-pack proof directory: the map wrote the fragment in
        # (partition_cell, ...) order, so a job's cells occupy a contiguous run.
        wanted: dict[int, tuple[list[int], dict[str, Any]]] = {}
        for row_group in pack["directory"]["row_groups"]:
            positions = sorted(
                {
                    position
                    for group in row_group["routing_groups"]
                    for position in positions_of_cell.get(group["partition_cell"], ())
                }
            )
            if positions:
                wanted[row_group["index"]] = (positions, row_group["binding"])
        if not wanted:
            continue
        parquet = pq.ParquetFile(store.path(pack["object"]["key"]))
        fragments_opened += 1
        for index in sorted(wanted):
            positions, expected = wanted[index]
            group_selected: dict[int, list[dict[str, Any]]] = {
                position: [] for position in positions
            }
            group_discarded: dict[int, list[dict[str, Any]]] = {
                position: [] for position in positions
            }
            for batch in parquet.iter_batches(
                batch_size=MAX_IPC_BATCH_ROWS,
                row_groups=[index],
                use_threads=False,
            ):
                maximum_batch_rows = max(maximum_batch_rows, batch.num_rows)
                if not initialized:
                    # A term column with this name would NOT raise on DuckDB
                    # 1.5.1: the injected tag is silently renamed to
                    # `<name>_1`, so `* EXCLUDE(<name>)` drops the DATA column
                    # and the emit predicate filters on it instead -- every
                    # partition then publishes the wrong rows while every
                    # binding still matches. Nothing downstream could see it,
                    # so refuse the collision here.
                    if PARTITION_INDEX_COLUMN in batch.schema.names:
                        raise ValueError(
                            f"Places term rows carry a {PARTITION_INDEX_COLUMN} column, "
                            "which collides with the reducer's partition tag"
                        )
                    connection.register("places_selected_batch", batch)
                    connection.execute(
                        "CREATE TABLE selected AS SELECT *, 0::INTEGER AS "
                        f"{PARTITION_INDEX_COLUMN} FROM places_selected_batch WHERE false"
                    )
                    connection.unregister("places_selected_batch")
                    initialized = True
                for position in positions:
                    mask = _partition_mask(batch, partitions[position][1])
                    selected = batch.filter(mask)
                    discarded = batch.filter(pc.invert(mask))
                    group_selected[position].append(
                        binding_for_table(pa.Table.from_batches([selected]))
                    )
                    group_discarded[position].append(
                        binding_for_table(pa.Table.from_batches([discarded]))
                    )
                    if selected.num_rows:
                        connection.register("places_selected_batch", selected)
                        try:
                            connection.execute(
                                "INSERT INTO selected SELECT *, "
                                f"{position} FROM places_selected_batch"
                            )
                        finally:
                            connection.unregister("places_selected_batch")
            claimed = []
            for position in positions:
                selected_binding = A.combine_bindings(group_selected[position])
                discarded_binding = A.combine_bindings(group_discarded[position])
                if (
                    A.combine_bindings([selected_binding, discarded_binding])
                    != expected
                ):
                    raise ValueError("Places selected/discarded row-group proof differs")
                selected_bindings[position].append(selected_binding)
                claimed.append(selected_binding)
                reconciled[position].append(
                    {
                        "pack_sha256": pack["object"]["sha256"],
                        "row_group": index,
                        "selected": selected_binding,
                        "discarded": discarded_binding,
                    }
                )
            # Exact ownership, checked per row group rather than argued: when the
            # job owns the fragment's whole bucket, the partitions it holds must
            # claim EVERY row of the row group, each exactly once. Summing the
            # per-partition selected bindings and comparing to the row group's
            # recorded binding catches a dropped cell and a double-claimed cell
            # in both digest lanes, which a row count alone would not.
            #
            # What this does NOT catch, stated plainly: the sum is additive and
            # order-independent, so SWAPPING two of this job's partitions leaves
            # it satisfied. That case is caught downstream instead -- each
            # partition's selected binding must equal the plan's, and the emitted
            # bytes must digest back to it (`_emit_partition`).
            if require_complete and A.combine_bindings(claimed) != expected:
                raise ValueError(
                    "Places bucket-range reduce left rows of a fragment unclaimed "
                    "or claimed them twice"
                )
    if not initialized:
        raise ValueError("Places reducer selected no row groups")
    return {
        "reconciled": reconciled,
        "selected_bindings": [
            A.combine_bindings(bindings) for bindings in selected_bindings
        ],
        "maximum_batch_rows": maximum_batch_rows,
        "fragments_opened": fragments_opened,
    }


def _owned_row_check(
    connection: Any, position: int, partition: dict[str, Any]
) -> dict[str, int]:
    """Measure the emit predicate's result set DuckDB-side, before writing it.

    Every binding in this file is computed pyarrow-side while ingesting; the
    published bytes come out DuckDB-side through
    ``WHERE __reduce_partition = <position>``. Nothing used to connect the two,
    so a predicate that selected the WRONG partition produced wrong serving
    artifacts that every check -- including finalize's reconciliation, which sums
    the pyarrow-side bindings -- accepted.

    So measure the predicate's own result set, in SQL, against the partition's
    IDENTITY: its cell, its token-hash ownership prefix, and the plan's row count.
    Those three pin the set exactly -- the owned rows are precisely the ingested
    rows with that cell and prefix -- so a predicate selecting anything else
    fails here rather than at publication.
    """
    ownership = partition.get("ownership", {"depth": 0, "prefix": 0})
    cell = partition["partition_cell"].replace("'", "''")
    foreign_prefix = "0"
    if ownership.get("depth"):
        foreign_prefix = (
            f"count(*) FILTER (WHERE {_prefix_sql(ownership['depth'])} != "
            f"{ownership['prefix']})"
        )
    records, foreign_cell, foreign_prefix_rows = connection.execute(
        f"SELECT count(*), count(*) FILTER (WHERE partition_cell != '{cell}'), "
        f"{foreign_prefix} FROM selected WHERE {PARTITION_INDEX_COLUMN}={position}"
    ).fetchone()
    return {
        "records": int(records),
        "foreign_cell_rows": int(foreign_cell),
        "foreign_prefix_rows": int(foreign_prefix_rows),
    }


def _binding_for_parquet(path: Path) -> dict[str, Any]:
    """Dual-lane binding read back from a written parquet's digest columns."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    total = A.zero_binding()
    parquet = pq.ParquetFile(path)
    for batch in parquet.iter_batches(
        batch_size=MAX_IPC_BATCH_ROWS,
        columns=["semantic_digest_a", "semantic_digest_b"],
    ):
        total = A.combine_bindings(
            [total, binding_for_table(pa.Table.from_batches([batch]))]
        )
    return total


def _emit_partition(
    *,
    connection: Any,
    position: int,
    partition: dict[str, Any],
    workspace: Path,
    store: A.LocalObjectStore,
    encoder_binary: Path,
    verifier_binary: Path,
    limits: Limits,
    reconciled: list[dict[str, Any]],
    selected_binding: dict[str, Any],
    maximum_batch_rows: int,
    ingest_evidence: dict[str, Any],
    ingestion_scope: str,
    wall_seconds: float,
) -> dict[str, Any]:
    """Encode, verify and store ONE partition out of the ingested table."""
    if not reconciled:
        raise ValueError("Places reducer selected no row groups for a partition")
    if selected_binding != partition["binding"]:
        raise ValueError("Places reducer selected binding differs from plan")
    columns = f"* EXCLUDE({PARTITION_INDEX_COLUMN})"
    owned = f"FROM selected WHERE {PARTITION_INDEX_COLUMN}={position}"
    leaf = workspace / f"leaf-{position:05d}.parquet"
    arrow = workspace / f"leaf-{position:05d}.arrow"
    with _reduce_watchdog(workspace, limits, connection, wall_seconds) as watchdog:
        predicate = _owned_row_check(connection, position, partition)
        if (
            predicate["records"] != partition["binding"]["records"]
            or predicate["foreign_cell_rows"]
            or predicate["foreign_prefix_rows"]
        ):
            raise ValueError(
                "Places reduce emit predicate does not select this partition: "
                f"{predicate} against plan records "
                f"{partition['binding']['records']}"
            )
        connection.execute(
            f"COPY (SELECT {columns} {owned} ORDER BY {TOTAL_ORDER}) TO '{leaf}' "
            f"(FORMAT PARQUET, COMPRESSION ZSTD, COMPRESSION_LEVEL 6, "
            f"ROW_GROUP_SIZE {limits.parquet_row_group_rows}, PARQUET_VERSION V2, "
            "PRESERVE_ORDER true)"
        )
        # Bind the BYTES, not the intent: digest the leaf that is about to be
        # stored and require it to reproduce the plan's binding on both lanes.
        # This is the only step that ties a published object to the plan; the
        # SQL check above is a cheap, independent second opinion on the same
        # predicate.
        emitted_binding = _binding_for_parquet(leaf)
        if emitted_binding != partition["binding"]:
            raise ValueError(
                "Places reduce leaf bytes do not digest back to the plan binding"
            )
        # The serving stream is derived from the leaf that was just PROVEN rather
        # than from a second, unproven predicate over `selected`. Same rows, same
        # TOTAL_ORDER, so the encoder input is unchanged -- but there is now
        # exactly one place where the partition's row set is decided.
        serving_rows = A.write_arrow_query(
            connection,
            f"SELECT * FROM read_parquet('{leaf}') QUALIFY row_number() OVER "
            f"({SERVING_PARTITION} ORDER BY {SERVING_ORDER})"
            f"<={limits.maximum_serving_candidates} ORDER BY {TOTAL_ORDER}",
            arrow,
            MAX_IPC_BATCH_ROWS,
        )
        if serving_rows > predicate["records"]:
            raise ValueError("Places reduce serving rows exceed the partition")
    serving_evidence = watchdog.evidence()
    routed = workspace / f"routed-{position:05d}.plrv"
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
    reduction = {
        "schema": REDUCE_SCHEMA,
        "partition": partition,
        "binding": selected_binding,
        "reconciled_row_groups": reconciled,
        # `scope` because a bucket-range job ingests every partition in its range
        # in one pass: these numbers describe that pass, not this partition. The
        # per-partition figures are `emit_verification` and `serving_evidence`.
        "streaming_ingestion": {
            "maximum_batch_rows": maximum_batch_rows,
            "full_table_read_all": False,
            "scope": ingestion_scope,
        },
        "serving_candidate_rows": serving_rows,
        "emit_verification": {
            **predicate,
            "leaf_binding": emitted_binding,
            "binds_published_bytes": True,
        },
        "leaf_object": store.put_content(leaf, "reduce/places-v1/leaves", ".parquet"),
        "routed_object": store.put_content(routed, "serve/places-v1/routed", ".plrv"),
        "encode_evidence": encode_evidence,
        "ingest_evidence": {**ingest_evidence, "scope": ingestion_scope},
        "serving_evidence": serving_evidence,
    }
    # The store already holds the bytes, so the workspace copies are dead weight.
    # A bucket-range job emits many partitions from one workspace, so keeping
    # them would make the scratch peak grow with the range rather than with the
    # largest partition.
    leaf.unlink(missing_ok=True)
    arrow.unlink(missing_ok=True)
    routed.unlink(missing_ok=True)
    return reduction


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
    """Reduce exactly one partition.

    Retained for the single-partition CLI path and for tests. The bucket-range
    reducer below is what the planner dispatches: this one has to consider every
    fragment in the marker set to find its own, which is the work a range
    consumer does once for a whole range.
    """
    _reduce_preconditions(plan, markers, [partition], limits)
    started = time.monotonic()
    scratch_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f"reduce-{partition['id']}-", dir=scratch_root
    ) as name:
        workspace = Path(name)
        connection = _reduce_connection(workspace, limits)
        try:
            with _reduce_watchdog(workspace, limits, connection) as watchdog:
                ingested = _reduce_ingest(
                    connection=connection,
                    fragments=[
                        pack for marker in markers for pack in marker["packs"]
                    ],
                    partitions=[(0, partition)],
                    store=store,
                    require_complete=False,
                )
            return _emit_partition(
                connection=connection,
                position=0,
                partition=partition,
                workspace=workspace,
                store=store,
                encoder_binary=encoder_binary,
                verifier_binary=verifier_binary,
                limits=limits,
                reconciled=ingested["reconciled"][0],
                selected_binding=ingested["selected_bindings"][0],
                maximum_batch_rows=ingested["maximum_batch_rows"],
                ingest_evidence=watchdog.evidence(),
                ingestion_scope="partition",
                wall_seconds=_remaining_wall(started, limits),
            )
        finally:
            connection.close()


def reduce_bucket_range(
    *,
    bucket_start: int,
    bucket_end: int,
    plan: dict[str, Any],
    markers: list[dict[str, Any]],
    store: A.LocalObjectStore,
    scratch_root: Path,
    encoder_binary: Path,
    verifier_binary: Path,
    limits: Limits,
) -> dict[str, Any]:
    """Reduce every partition whose cell falls in an INCLUSIVE bucket range.

    This is the shape the shuffle was built for. `pack_id` IS the shuffle bucket
    and packs are ordered by (shuffle_bucket, partition_cell, feature_id), so a
    range consumer's input is the contiguous set of fragments whose bucket lies
    in its range -- derived from the markers, not re-derived per partition -- and
    each is opened exactly once.

    Ownership is exact for ANY partition of the bucket space into ranges: a cell
    hashes to one bucket, a bucket lies in one range, so every partition is
    emitted once and none is dropped. An empty range is legal and free.
    """
    bits = limits.shuffle_bucket_bits
    start, end = validate_bucket_range(bucket_start, bucket_end, bits)
    owned = bucket_range_partitions(
        plan, bucket_start=start, bucket_end=end, bits=bits
    )
    fragments = bucket_range_fragments(
        markers, bucket_start=start, bucket_end=end, bits=bits
    )
    _reduce_preconditions(plan, markers, [item[1] for item in owned], limits)
    summary = {
        "schema": REDUCE_RANGE_SCHEMA,
        "shuffle_bucket_bits": bits,
        "bucket_start": start,
        "bucket_end": end,
        "partition_indexes": [index for index, _ in owned],
        "fragment_keys": [pack["object"]["key"] for pack in fragments],
    }
    if not owned:
        # No cell hashed into this range. A range with no partitions must also
        # have no fragments -- if it had one, a cell would be missing from the
        # plan and the whole ownership argument would be false.
        if fragments:
            raise ValueError(
                "Places bucket range holds map fragments but no plan partitions"
            )
        return {
            **summary,
            "fragments_opened": 0,
            "reductions": [],
            "binding": A.zero_binding(),
        }
    started = time.monotonic()
    scratch_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f"reduce-b{start:05d}-{end:05d}-", dir=scratch_root
    ) as name:
        workspace = Path(name)
        connection = _reduce_connection(workspace, limits)
        try:
            with _reduce_watchdog(workspace, limits, connection) as watchdog:
                ingested = _reduce_ingest(
                    connection=connection,
                    fragments=fragments,
                    partitions=owned,
                    store=store,
                    require_complete=True,
                )
            ingest_evidence = watchdog.evidence()
            reductions = [
                _emit_partition(
                    connection=connection,
                    position=position,
                    partition=partition,
                    workspace=workspace,
                    store=store,
                    encoder_binary=encoder_binary,
                    verifier_binary=verifier_binary,
                    limits=limits,
                    reconciled=ingested["reconciled"][position],
                    selected_binding=ingested["selected_bindings"][position],
                    maximum_batch_rows=ingested["maximum_batch_rows"],
                    ingest_evidence=ingest_evidence,
                    ingestion_scope="bucket-range-job",
                    # The REMAINING job budget, so the job -- not each serving
                    # stage -- is what the wall cap bounds.
                    wall_seconds=_remaining_wall(started, limits),
                )
                for position, (_plan_index, partition) in enumerate(owned)
            ]
        finally:
            connection.close()
    binding = A.combine_bindings([item["binding"] for item in reductions])
    if binding != A.combine_bindings([item[1]["binding"] for item in owned]):
        raise ValueError("Places bucket-range reduce binding differs from the plan")
    return {
        **summary,
        "fragments_opened": ingested["fragments_opened"],
        "reductions": reductions,
        "binding": binding,
        "ingest_evidence": {**ingest_evidence, "scope": "bucket-range-job"},
    }


def validate_complete_reduction(
    plan: dict[str, Any], reductions: list[dict[str, Any]]
) -> dict[str, Any]:
    """Reconcile a COMPLETE set of Places reductions against the genesis plan.

    The Places counterpart of `address_construction_v1.validate_complete_reduction`,
    and the reason finalize no longer reports `reconciles: true` from a literal.
    Three independent things are checked, because the total binding alone does not
    imply any of them:

    * the partition ID set matches the plan's exactly -- no missing, extra, or
      duplicated partition. A duplicate that replaces a missing partition of the
      same size leaves the summed binding untouched, so the sum cannot see it.
    * each reduction's binding equals the binding the PLAN recorded for that
      partition, so two partitions cannot swap their outputs.
    * the combined binding equals the plan's, which is what the previous check
      did on its own.

    Raises `ValueError` (as the address version does) rather than returning a
    flag: a reduction set that does not reconcile must not be publishable.
    """
    expected_ids = [partition["id"] for partition in plan["partitions"]]
    actual_ids = [item["partition"]["id"] for item in reductions]
    if len(actual_ids) != len(set(actual_ids)) or sorted(actual_ids) != sorted(
        expected_ids
    ):
        raise ValueError(
            "Places reduction has missing, extra, or duplicate partitions"
        )
    planned = {
        partition["id"]: partition["binding"] for partition in plan["partitions"]
    }
    for item in reductions:
        partition_id = item["partition"]["id"]
        if item["binding"] != planned[partition_id]:
            raise ValueError(
                f"Places reduction binding for {partition_id} differs from the "
                "binding the genesis plan recorded for that partition"
            )
    binding = A.combine_bindings([item["binding"] for item in reductions])
    if binding != plan["binding"]:
        raise ValueError(
            "Places complete reduction binding differs from the genesis plan"
        )
    return {"partitions": len(reductions), "binding": binding, "reconciles": True}


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
                    "sha256": stored["sha256"],
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
            # `sha256`/`bytes` are carried so finalize can check the file it is
            # about to publish against the identity THIS phase recorded, the same
            # way it already can for reduce artifacts and per-record packs. Without
            # them a head shard was publishable on the strength of its filename
            # alone.
            "shard_objects": [
                {
                    "shard_id": entry["shard_id"],
                    "key": entry["key"],
                    "sha256": entry["sha256"],
                    "bytes": entry["bytes"],
                }
                for entry in shard_entries
            ],
            "verify_evidence": verify,
        }
