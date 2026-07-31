#!/usr/bin/env python3
"""Read-only, representative planet Address reverse-index probe.

The input is the exact compact plan produced by a successful reverse-v2 dry
run.  Every directory and selected source pack is hydrated through the
content-addressed construction staging reader and independently checked against
the identity recorded in that plan.  The probe writes only ephemeral local
scratch and its JSON report: it has no publication store and no marker writer.

The globally densest level-8 cell is selected deterministically from the full
directory set.  Only packs whose authenticated directory contains that cell
are then hydrated.  The production reverse encoder and verifier build one local
shard so the report can measure dictionary cardinality, framing bytes and
resources on real planet data without publishing a reverse artifact.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


R2 = _load("reverse_address_probe_r2", "scripts/reverse_r2_v1.py")
STAGING = R2.STAGING
ADDRESS = R2.ADDRESS
REVERSE = R2.REVERSE

SCHEMA = "overture-reverse-address-representative-probe-v1"
DICTIONARY_FIELDS = (
    "display_country",
    "postal_city",
    "postcode",
    "street",
    "number",
    "unit",
    "address_levels",
)
RANGE_WIDTH = 16
RANGE_COUNT = 16
OUTPUT_CAP_BYTES = 3 * 1024**3
ADDRESS_DICTIONARY_FORMAT = REVERSE.ADDRESS_DICTIONARY_MAGIC.decode("ascii")
ADDRESS_DICTIONARY_HEADER_BYTES = len(REVERSE.ADDRESS_DICTIONARY_MAGIC) + 4
# ARDX0002 field header: u32 count + u8 code width.
ADDRESS_DICTIONARY_FIELD_HEADER_BYTES = 5
ADDRESS_DICTIONARY_TEXT_LENGTH_BYTES = 2

# Preserved real Seattle execution evidence in
# docs/plans/2026-07-25-reverse-v2-design.md: the production dictionary encoder
# wrote 6,251,653 bytes for 104,928 rows.  Keep the exact rational rather than a
# rounded 59.5804-byte decimal.
SEATTLE_BASELINE_BYTES = 6_251_653
SEATTLE_BASELINE_RECORDS = 104_928

# Reserve essentially all of the workflow's documented ~53% margin over the
# Seattle projection as uncertainty rather than spending it.  A rational 3/2
# is deterministic, easy to audit, and leaves the remaining ~3 percentage
# points for integer rounding.  The projection basis is the worse of Seattle
# and the independently measured global densest cell.
HEADROOM_NUMERATOR = 3
HEADROOM_DENOMINATOR = 2


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ReadOnlyStagedStore:
    """Expose only verified hydration, release and evidence.

    Deliberately omitting ``put_content`` and ``write_marker_last`` makes a
    future accidental publication call fail before it reaches the backend.
    """

    def __init__(
        self,
        *,
        cache_root: Path,
        request_sha256: str,
        staging_root: Path | None,
        staging_bucket: str | None,
        staging_endpoint_url: str | None,
    ):
        local = ADDRESS.LocalObjectStore(cache_root)
        backend = STAGING.staging_backend(
            store_root=staging_root,
            bucket=staging_bucket,
            endpoint_url=staging_endpoint_url,
        )
        self._store = STAGING.StagedObjectStore(
            local,
            backend,
            STAGING.staging_prefix(request_sha256, "addresses"),
        )

    @property
    def root(self) -> Path:
        return self._store.root

    def path(self, key: str) -> Path:
        return self._store.path(key)

    def release(self, key: str) -> None:
        self._store.release(key)

    def evidence(self) -> dict[str, Any]:
        return self._store.evidence()


def range_counts(plan: dict[str, Any]) -> list[dict[str, int]]:
    counts = [
        {
            "range_id": index,
            "bucket_start": index * RANGE_WIDTH,
            "bucket_end": index * RANGE_WIDTH + RANGE_WIDTH - 1,
            "records": 0,
            "source_packs": 0,
            "source_bytes": 0,
        }
        for index in range(RANGE_COUNT)
    ]
    for pack in plan["packs"]:
        item = counts[pack["shuffle_bucket"] // RANGE_WIDTH]
        item["records"] += pack["records"]
        item["source_packs"] += 1
        item["source_bytes"] += pack["object"]["bytes"]
    if sum(item["records"] for item in counts) != plan["expected_records"]:
        raise ValueError("reverse Address range counts do not reconcile with the plan")
    return counts


def select_densest_cell(cell_counts: dict[str, int]) -> tuple[str, int]:
    if not cell_counts:
        raise ValueError("reverse Address probe found no populated cells")
    for cell, records in cell_counts.items():
        REVERSE.cell_yx(cell)
        if isinstance(records, bool) or not isinstance(records, int) or records < 1:
            raise ValueError("reverse Address probe cell count is invalid")
    # Ascending cell name is the explicit tie break.
    return min(cell_counts.items(), key=lambda item: (-item[1], item[0]))


def conservative_projection(
    *,
    ranges: list[dict[str, int]],
    probe_records: int,
    probe_bytes: int,
    cap_bytes: int = OUTPUT_CAP_BYTES,
) -> dict[str, Any]:
    if probe_records < 1 or probe_bytes < 1 or cap_bytes < 1:
        raise ValueError("reverse Address projection inputs must be positive")
    if (
        probe_bytes * SEATTLE_BASELINE_RECORDS
        >= SEATTLE_BASELINE_BYTES * probe_records
    ):
        basis_name = "global_densest_cell"
        basis_bytes = probe_bytes
        basis_records = probe_records
    else:
        basis_name = "seattle_real_slice"
        basis_bytes = SEATTLE_BASELINE_BYTES
        basis_records = SEATTLE_BASELINE_RECORDS

    projected = []
    for item in ranges:
        numerator = item["records"] * basis_bytes * HEADROOM_NUMERATOR
        denominator = basis_records * HEADROOM_DENOMINATOR
        projected_bytes = (numerator + denominator - 1) // denominator
        projected.append({**item, "projected_output_bytes": projected_bytes})
    maximum = max(
        projected,
        key=lambda item: (
            item["projected_output_bytes"],
            -item["range_id"],
        ),
    )
    aggregate = sum(item["projected_output_bytes"] for item in projected)
    return {
        "method": "max_observed_bytes_per_record_times_three_halves",
        "basis": basis_name,
        "basis_bytes": basis_bytes,
        "basis_records": basis_records,
        "basis_bytes_per_record": basis_bytes / basis_records,
        "headroom_numerator": HEADROOM_NUMERATOR,
        "headroom_denominator": HEADROOM_DENOMINATOR,
        "justification": (
            "Use the worse whole-shard byte rate from the preserved Seattle real-data "
            "slice and the authenticated global densest cell, then reserve 50% empirical "
            "headroom. This consumes essentially all of the workflow's documented ~53% "
            "margin over the Seattle point estimate while leaving integer-rounding margin."
        ),
        "limitation": (
            "This is a representative empirical projection, not a mathematical upper "
            "bound over every unencoded cell. Execute remains protected independently by "
            "the confirmation-bound 3 GiB hard failure cap on every range."
        ),
        "cap_bytes_per_range": cap_bytes,
        "ranges": projected,
        "maximum_range": maximum,
        "aggregate_projected_output_bytes": aggregate,
        "within_cap": maximum["projected_output_bytes"] <= cap_bytes,
    }


def _verified_json(
    store: ReadOnlyStagedStore, identity: dict[str, Any], *, what: str
) -> dict[str, Any]:
    path = R2.verified_path(store, identity, what=what)
    try:
        return json.loads(path.read_text())
    finally:
        store.release(identity["key"])


def discover_density(
    *,
    plan: dict[str, Any],
    store: ReadOnlyStagedStore,
    started: float | None = None,
    limits: Any | None = None,
) -> tuple[dict[str, int], dict[str, set[str]], dict[str, Any]]:
    cell_counts: dict[str, int] = {}
    cells_by_pack: dict[str, set[str]] = {}
    directory_cache: dict[str, dict[str, Any]] = {}
    unique_directory_bytes = 0

    for pack in plan["packs"]:
        if started is not None and limits is not None:
            R2.remaining_wall(started, limits)
        directory_identity = pack["directory_object"]
        directory_key = directory_identity["key"]
        directory = directory_cache.get(directory_key)
        if directory is None:
            directory = _verified_json(
                store,
                directory_identity,
                what="reverse Address probe directory",
            )
            directory_cache[directory_key] = directory
            unique_directory_bytes += directory_identity["bytes"]
        validated = R2.validate_pack_metadata(
            {**pack, "directory": directory},
            family="addresses",
            task_id=pack["task_id"],
        )
        names: set[str] = set()
        for cell in validated["directory"]["cells"]:
            name = cell["partition_cell"]
            names.add(name)
            cell_counts[name] = cell_counts.get(name, 0) + cell["records"]
        cells_by_pack[pack["object"]["key"]] = names

    if sum(cell_counts.values()) != plan["expected_records"]:
        raise ValueError(
            "reverse Address directory cell counts do not reconcile with the plan"
        )
    return (
        cell_counts,
        cells_by_pack,
        {
            "directory_references": len(plan["packs"]),
            "unique_directories": len(directory_cache),
            "unique_directory_bytes": unique_directory_bytes,
            "records": sum(cell_counts.values()),
            "populated_cells": len(cell_counts),
        },
    )


def dictionary_cardinalities(shard: Any) -> dict[str, int]:
    dictionaries = getattr(shard, "_address_dictionary", None)
    if (
        shard.family != "addresses"
        or not isinstance(dictionaries, list)
        or len(dictionaries) != len(DICTIONARY_FIELDS)
    ):
        raise ValueError("reverse Address probe shard has no complete dictionary")
    values = {
        field: len(dictionary)
        for field, dictionary in zip(
            DICTIONARY_FIELDS, dictionaries, strict=True
        )
    }
    # ARDX0002 sizes each field's codes from its own cardinality, so there is no
    # upper ceiling here: the densest planet cell carries 96,738 streets, and the
    # real bound is the 8 MiB dictionary cap checked separately. Every field
    # still holds at least one value because the encoder codes every row.
    if any(count < 1 for count in values.values()):
        raise ValueError("reverse Address probe dictionary field is empty")
    return values


def pre_encoding_dictionary_metrics(connection: Any) -> dict[str, Any]:
    """Measure the exact ARDX0002 dictionary shape before invoking Rust."""
    # One aggregation at a time keeps the diagnostic bounded even when several
    # fields have millions of distinct values.
    fields = {}
    for field in DICTIONARY_FIELDS:
        if field == "address_levels":
            source = (
                "SELECT DISTINCT address_level AS value "
                "FROM reverse_probe, "
                "UNNEST(address_levels) AS levels(address_level) "
                "WHERE address_level IS NOT NULL"
            )
        else:
            source = (
                f"SELECT DISTINCT {field} AS value FROM reverse_probe "
                f"WHERE {field} IS NOT NULL"
            )
        cardinality, value_bytes, max_value_bytes = connection.execute(
            "SELECT count(*), "
            "coalesce(sum(octet_length(encode(value))), 0), "
            "coalesce(max(octet_length(encode(value))), 0) "
            f"FROM ({source})"
        ).fetchone()
        cardinality = int(cardinality)
        value_bytes = int(value_bytes)
        fields[field] = {
            "cardinality": cardinality,
            "utf8_value_bytes": value_bytes,
            "encoded_entry_bytes": (
                value_bytes
                + cardinality * ADDRESS_DICTIONARY_TEXT_LENGTH_BYTES
            ),
            "max_utf8_value_bytes": int(max_value_bytes),
        }
    field_header_bytes = (
        len(DICTIONARY_FIELDS) * ADDRESS_DICTIONARY_FIELD_HEADER_BYTES
    )
    encoded_entry_bytes = sum(
        metrics["encoded_entry_bytes"] for metrics in fields.values()
    )
    total_bytes = (
        ADDRESS_DICTIONARY_HEADER_BYTES
        + field_header_bytes
        + encoded_entry_bytes
    )
    return {
        "format": ADDRESS_DICTIONARY_FORMAT,
        "header_bytes": ADDRESS_DICTIONARY_HEADER_BYTES,
        "field_header_bytes": field_header_bytes,
        "encoded_entry_bytes": encoded_entry_bytes,
        "total_bytes": total_bytes,
        "serving_cap_bytes": R2.MAX_ADDRESS_DICTIONARY_BYTES,
        "exceeds_serving_cap": total_bytes > R2.MAX_ADDRESS_DICTIONARY_BYTES,
        "fields": fields,
    }


def write_result(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(R2.canonical_json(result) + b"\n")


def run_probe(args: argparse.Namespace) -> dict[str, Any]:
    import duckdb
    import pyarrow.ipc as ipc

    started = time.monotonic()
    if (
        len(args.expected_plan_sha256) != 64
        or any(
            character not in "0123456789abcdef"
            for character in args.expected_plan_sha256
        )
    ):
        raise ValueError("expected plan SHA-256 is not canonical")
    if sha256_file(args.plan) != args.expected_plan_sha256:
        raise ValueError("reverse Address plan bytes differ from the expected SHA-256")
    plan = R2.validate_plan(json.loads(args.plan.read_text()), family="addresses")
    limits = ADDRESS.Limits(
        max_rss_bytes=args.max_rss_bytes,
        max_scratch_bytes=args.max_scratch_bytes,
        max_output_bytes=args.max_output_bytes,
        wall_seconds=args.wall_seconds,
        duckdb_memory_limit=args.duckdb_memory_limit,
        duckdb_threads=args.duckdb_threads,
        required_duckdb_version=args.required_duckdb_version,
        allow_unpinned_duckdb=args.allow_unpinned_duckdb,
    )
    limits.validate()
    ADDRESS.require_duckdb_runtime(duckdb, limits)
    store = ReadOnlyStagedStore(
        cache_root=args.store_root,
        request_sha256=plan["request_sha256"],
        staging_root=args.staging_root,
        staging_bucket=args.staging_bucket,
        staging_endpoint_url=args.staging_endpoint_url,
    )
    ranges = range_counts(plan)
    cell_counts, cells_by_pack, directory_evidence = discover_density(
        plan=plan, store=store, started=started, limits=limits
    )
    dense_cell, dense_records = select_densest_cell(cell_counts)
    dense_bucket = R2.cell_bucket(dense_cell)
    selected = [
        pack
        for pack in plan["packs"]
        if dense_cell in cells_by_pack[pack["object"]["key"]]
    ]
    if (
        not selected
        or any(pack["shuffle_bucket"] != dense_bucket for pack in selected)
    ):
        raise ValueError("densest reverse Address cell has inconsistent source packs")

    args.scratch_dir.mkdir(parents=True, exist_ok=True)
    source_objects = [pack["object"] for pack in selected]
    source_bytes = sum(identity["bytes"] for identity in source_objects)

    with tempfile.TemporaryDirectory(
        prefix="reverse-address-probe-", dir=args.scratch_dir
    ) as temporary:
        workspace = Path(temporary)
        duckdb_scratch = workspace / "duckdb-spill"
        duckdb_scratch.mkdir()
        connection = duckdb.connect(str(workspace / "probe.duckdb"))
        connection.execute(f"SET memory_limit = '{limits.duckdb_memory_limit}'")
        connection.execute(f"SET threads = {limits.duckdb_threads}")
        connection.execute(f"SET temp_directory = '{duckdb_scratch}'")
        connection.execute(
            "SET max_temp_directory_size = "
            f"'{ADDRESS.duckdb_temp_limit(limits.max_scratch_bytes)}'"
        )
        roots = [workspace, store.root]
        watchdog = ADDRESS.StageWatchdog(
            roots,
            R2.bounded_limits(
                limits, wall_seconds=R2.remaining_wall(started, limits)
            ),
            connection,
        )
        encode_evidence: dict[str, Any]
        verify_evidence: dict[str, Any]
        encoder_failure: dict[str, Any] | None = None
        encoder_succeeded = False
        try:
            with watchdog:
                columns = ", ".join(R2.R1.ADDRESS_COLUMNS)
                for position, pack in enumerate(selected):
                    if R2.remaining_wall(started, limits) <= 0:
                        raise RuntimeError(
                            "reverse Address probe exhausted its wall budget"
                        )
                    pack_path = R2.verified_path(
                        store,
                        pack["object"],
                        what="reverse Address probe source pack",
                    )
                    try:
                        R2.staged_resident_guard(store, limits)
                        action = (
                            "CREATE TABLE reverse_probe AS"
                            if position == 0
                            else "INSERT INTO reverse_probe"
                        )
                        connection.execute(
                            f"{action} SELECT {columns} "
                            f"FROM read_parquet([{R2._sql_paths([pack_path])}]) "
                            f"WHERE partition_cell = '{dense_cell}'"
                        )
                    finally:
                        store.release(pack["object"]["key"])

                loaded = int(
                    connection.execute(
                        "SELECT count(*) FROM reverse_probe"
                    ).fetchone()[0]
                )
                if loaded != dense_records:
                    raise ValueError(
                        "densest reverse Address cell rows differ from directories"
                    )
                observed = connection.execute(
                    "SELECT min(partition_cell), max(partition_cell) "
                    "FROM reverse_probe"
                ).fetchone()
                if observed != (dense_cell, dense_cell):
                    raise ValueError("reverse Address probe loaded another cell")

                dictionary_metrics = pre_encoding_dictionary_metrics(connection)
                cardinalities = {
                    field: metrics["cardinality"]
                    for field, metrics in dictionary_metrics["fields"].items()
                }
                level = REVERSE.sub_cell_level(
                    dense_records,
                    dense_cell,
                    REVERSE.DEPTH_FAMILY_BY_SERVING["addresses"],
                )
                arrow = workspace / "densest.arrow"
                arrow_rows = ADDRESS.write_arrow_query(
                    connection,
                    R2._cell_query("addresses", dense_cell, level).replace(
                        "reverse_rows", "reverse_probe"
                    ),
                    arrow,
                    65_536,
                )
                if arrow_rows != dense_records:
                    raise ValueError(
                        "reverse Address probe Arrow rows do not reconcile"
                    )
                with arrow.open("rb") as source:
                    actual_schema = ipc.open_stream(source).schema
                if not actual_schema.equals(
                    R2.R1.input_schema("addresses"), check_metadata=False
                ):
                    raise ValueError("reverse Address probe Arrow schema differs")

                shard_path = workspace / f"addresses-{dense_cell}.plrx"
                sidecar_path = workspace / "densest.digest.json"
                encode_evidence = ADDRESS.run_bounded(
                    [
                        str(args.encoder_binary),
                        "--input",
                        str(arrow),
                        "--output",
                        str(shard_path),
                        "--family",
                        "addresses",
                        "--cell",
                        dense_cell,
                        "--records",
                        str(dense_records),
                        "--digest-out",
                        str(sidecar_path),
                    ],
                    scratch_roots=roots,
                    limits=R2.bounded_limits(
                        limits, wall_seconds=R2.remaining_wall(started, limits)
                    ),
                )
                encoder_succeeded = True
                verify_evidence = ADDRESS.run_bounded(
                    [
                        str(args.verifier_binary),
                        "--input",
                        str(shard_path),
                        "--family",
                        "addresses",
                        "--cell",
                        dense_cell,
                        "--records",
                        str(dense_records),
                        "--digest",
                        str(sidecar_path),
                    ],
                    scratch_roots=roots,
                    limits=R2.bounded_limits(
                        limits, wall_seconds=R2.remaining_wall(started, limits)
                    ),
                )
                shard = REVERSE.ReverseShard(shard_path.read_bytes())
                sidecar = json.loads(sidecar_path.read_text())
                total_bytes = shard_path.stat().st_size
                if dictionary_cardinalities(shard) != cardinalities:
                    raise ValueError(
                        "pre-encoding Address dictionary cardinalities "
                        "differ from the encoded shard"
                    )
                if (
                    shard.cell != dense_cell
                    or shard.records != loaded
                    or shard.dictionary_bytes != sidecar.get("dictionary_bytes")
                    or shard.dictionary_bytes != dictionary_metrics["total_bytes"]
                    or shard.dictionary_bytes > R2.MAX_ADDRESS_DICTIONARY_BYTES
                    or total_bytes > limits.max_output_bytes
                ):
                    raise ValueError(
                        "reverse Address probe shard evidence does not reconcile"
                    )
                framing = {
                    "header_bytes": R2.SHARD_HEADER_BYTES,
                    "dictionary_bytes": shard.dictionary_bytes,
                    "payload_bytes": shard.index_offset - shard.payload_offset,
                    "index_bytes": total_bytes - shard.index_offset,
                    "total_bytes": total_bytes,
                }
                if sum(
                    framing[key]
                    for key in (
                        "header_bytes",
                        "dictionary_bytes",
                        "payload_bytes",
                        "index_bytes",
                    )
                ) != total_bytes:
                    raise ValueError(
                        "reverse Address probe shard framing does not reconcile"
                    )
                leaves = len(shard.leaf_ranges())
        except subprocess.CalledProcessError as error:
            if encoder_succeeded:
                raise
            encoder_failure = {
                "status": "failed",
                "exit_code": error.returncode,
                "reason": "production reverse Address encoder exited nonzero",
            }
        finally:
            connection.close()

    staging_evidence = store.evidence()
    if (
        staging_evidence["staged_objects_published"] != 0
        or staging_evidence["staged_bytes_published"] != 0
        or staging_evidence["staged_peak_resident_bytes"] > limits.max_scratch_bytes
    ):
        raise ValueError("reverse Address probe violated its read-only staging bounds")
    base_result = {
        "schema": SCHEMA,
        "read_only": True,
        "family": "addresses",
        "request_sha256": plan["request_sha256"],
        "plan_sha256": args.expected_plan_sha256,
        "plan": {
            "records": plan["expected_records"],
            "packs": len(plan["packs"]),
            "source_bytes": sum(pack["object"]["bytes"] for pack in plan["packs"]),
        },
        "directory_scan": directory_evidence,
    }
    base_dense_cell = {
        "partition_cell": dense_cell,
        "shuffle_bucket": dense_bucket,
        "records": dense_records,
        "sub_cell_level": level,
        "source_packs": len(selected),
        "source_bytes": source_bytes,
        "source_objects": source_objects,
        "loaded_records": loaded,
        "dictionary_cardinalities": cardinalities,
        "pre_encoding_dictionary": dictionary_metrics,
        # ARDX0002 sizes each field's codes independently, so a cardinality
        # above 65,536 widens that field to four bytes instead of failing.
        # Report the chosen widths rather than an overflow list.
        "dictionary_code_widths": {
            field: REVERSE.address_code_width(count)
            for field, count in cardinalities.items()
        },
        "wide_code_fields": [
            field
            for field, count in cardinalities.items()
            if REVERSE.address_code_width(count) == 4
        ],
    }
    base_resources = {
        "whole_probe_wall_seconds": time.monotonic() - started,
        "watchdog": watchdog.evidence(),
        "staging": staging_evidence,
    }
    if encoder_failure is not None:
        result = {
            **base_result,
            "measurement_status": "encoder_failed",
            "densest_cell": {
                **base_dense_cell,
                "encoded_records": None,
                "framing": None,
                "leaves": None,
                "bytes_per_record": None,
            },
            "projection": None,
            "resources": {
                **base_resources,
                "encoder": encoder_failure,
                "verifier": {
                    "status": "not_run",
                    "reason": "production encoder did not produce a verified shard",
                },
            },
            "execute_gate": {
                "status": "blocked",
                "reason": "production reverse Address encoder exited nonzero",
            },
        }
        write_result(args.output, result)
        return result

    projection = conservative_projection(
        ranges=ranges,
        probe_records=dense_records,
        probe_bytes=framing["total_bytes"],
        cap_bytes=limits.max_output_bytes,
    )
    result = {
        **base_result,
        "measurement_status": "complete",
        "densest_cell": {
            **base_dense_cell,
            "encoded_records": shard.records,
            "framing": framing,
            "leaves": leaves,
            "bytes_per_record": framing["total_bytes"] / dense_records,
        },
        "projection": projection,
        "resources": {
            **base_resources,
            "encoder": encode_evidence,
            "verifier": verify_evidence,
        },
        "execute_gate": {
            "status": "pass" if projection["within_cap"] else "blocked",
            "reason": (
                "representative projection remains within the confirmation-bound "
                "3 GiB per-range hard cap"
                if projection["within_cap"]
                else "representative projection exceeds the confirmation-bound "
                "3 GiB per-range hard cap"
            ),
        },
    }
    write_result(args.output, result)
    return result


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--plan", type=Path, required=True)
    value.add_argument("--expected-plan-sha256", required=True)
    value.add_argument("--store-root", type=Path, required=True)
    source = value.add_mutually_exclusive_group(required=True)
    source.add_argument("--staging-root", type=Path)
    source.add_argument("--staging-bucket")
    value.add_argument("--staging-endpoint-url")
    value.add_argument("--scratch-dir", type=Path, required=True)
    value.add_argument("--encoder-binary", type=Path, required=True)
    value.add_argument("--verifier-binary", type=Path, required=True)
    value.add_argument("--output", type=Path, required=True)
    defaults = ADDRESS.Limits()
    value.add_argument("--max-rss-bytes", type=int, default=defaults.max_rss_bytes)
    value.add_argument(
        "--max-scratch-bytes", type=int, default=defaults.max_scratch_bytes
    )
    value.add_argument("--max-output-bytes", type=int, default=OUTPUT_CAP_BYTES)
    value.add_argument("--wall-seconds", type=float, default=2_400)
    value.add_argument("--duckdb-memory-limit", default=defaults.duckdb_memory_limit)
    value.add_argument("--duckdb-threads", type=int, default=defaults.duckdb_threads)
    value.add_argument(
        "--required-duckdb-version", default=defaults.required_duckdb_version
    )
    value.add_argument("--allow-unpinned-duckdb", action="store_true")
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.staging_bucket and not args.staging_endpoint_url:
        raise SystemExit("--staging-bucket requires --staging-endpoint-url")
    if args.staging_root and args.staging_endpoint_url:
        raise SystemExit("--staging-endpoint-url is only valid with --staging-bucket")
    result = run_probe(args)
    summary = {
        "output": str(args.output),
        "request_sha256": result["request_sha256"],
        "plan_sha256": result["plan_sha256"],
        "densest_cell": result["densest_cell"]["partition_cell"],
        "densest_records": result["densest_cell"]["records"],
        "dictionary_cardinalities": result["densest_cell"][
            "dictionary_cardinalities"
        ],
        "projected_ardx0001_dictionary_bytes": result["densest_cell"][
            "pre_encoding_dictionary"
        ]["total_bytes"],
        "ardx0001_dictionary_exceeds_serving_cap": result["densest_cell"][
            "pre_encoding_dictionary"
        ]["exceeds_serving_cap"],
        "measurement_status": result["measurement_status"],
        "execute_gate": result["execute_gate"]["status"],
    }
    if result["measurement_status"] == "complete":
        summary["dictionary_bytes"] = result["densest_cell"]["framing"][
            "dictionary_bytes"
        ]
        summary["projected_max_range_bytes"] = result["projection"]["maximum_range"][
            "projected_output_bytes"
        ]
    else:
        summary["encoder_exit_code"] = result["resources"]["encoder"]["exit_code"]
    print(
        json.dumps(summary, sort_keys=True)
    )
    return 0 if result["execute_gate"]["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
