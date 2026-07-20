#!/usr/bin/env python3
"""Build the exact bounded global Places ``head.phrp`` from planned fragments.

The builder performs two sequential passes with at most one fragment body open
or cached at once. Pass one stores only exact-token/prefix counts and the
bounded famous set. Pass two retains at most ``reader key cap × result cap``
ranked projections. It never stores the planet's Places rows or postings.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Iterator


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import global_v2_build_request  # noqa: E402
from experiment_places_compact_index import (  # noqa: E402
    Place,
    decode_record,
    encode_record,
    encode_varint,
    place_from_row,
)
from experiment_places_head_repack import (  # noqa: E402
    HEAD_ADMISSION_MARKER,
    HEAD_KEY_FAMILIES,
    MAGIC,
    PREAMBLE,
    READER_MAX_HEAD_ENTRY_BYTES,
    READER_MAX_HEAD_INDEX_BYTES,
    READER_MAX_HEAD_KEYS,
    READER_MAX_KEY_BYTES,
    encode_key_index,
)
from experiment_places_locality_head import (  # noqa: E402
    FAMOUS_PAIR_TOKEN_LIMIT,
    HEAD_PREFIX_LENGTHS,
    famous_name_brand_tokens,
    famous_pair_token_key,
    place_terms,
)
from global_v2_places_plan import (  # noqa: E402
    REQUIRED_PYARROW_VERSION,
    REQUIRED_PYTHON_VERSION,
    digest_value,
    request_sha256,
    require_exact,
    require_int,
    require_sha256,
    safe_artifact_path,
    sha256_file,
    validate_places_plan,
)
from global_v2_places_reduce import (  # noqa: E402
    PyArrowFragmentReader,
    _validate_fragment_row,
    parse_fetch_command,
    validate_fetch_command,
)


HEAD_REPORT_SCHEMA = "overture-global-v2-places-head-report-v1"
HEAD_VERSION = "1"
MAX_HEAD_SCRATCH_BYTES = 12_000_000_000
MAX_HEAD_COUNT_BATCH_KEYS = 200_000
MAX_HEAD_CANDIDATE_SLOTS = READER_MAX_HEAD_KEYS * 10
HEAD_MAX_OPEN_FRAGMENT_FILES = 1
MAX_HEAD_ENTRIES_BYTES = 2_000_000_000
MAX_HEAD_COUNT_BATCH_STRING_BYTES = 64_000_000
MAX_HEAD_FAMOUS_SERIALIZED_BYTES = 1_000_000_000
MAX_HEAD_ADMITTED_KEY_BYTES = READER_MAX_HEAD_INDEX_BYTES


FragmentReader = Callable[[dict[str, Any], Path], Iterator[dict[str, Any]]]


def _module_sha256(name: str) -> str:
    return sha256_file(SCRIPT_DIR / name)[0]


class _CountDatabase:
    def __init__(
        self,
        scratch_dir: Path,
        workspace_observer: Callable[[int], None] | None = None,
    ) -> None:
        scratch_dir.mkdir(parents=True, exist_ok=True)
        self.scratch_dir = scratch_dir
        descriptor, name = tempfile.mkstemp(
            prefix="places-head-counts-", suffix=".sqlite3", dir=scratch_dir
        )
        os.close(descriptor)
        self.path = Path(name)
        self.connection = sqlite3.connect(self.path)
        self.connection.execute("PRAGMA journal_mode=OFF")
        self.connection.execute("PRAGMA synchronous=OFF")
        self.connection.execute("PRAGMA temp_store=FILE")
        self.connection.execute("PRAGMA cache_size=-65536")
        self.page_size = self.connection.execute("PRAGMA page_size").fetchone()[0]
        requested_max_pages = MAX_HEAD_SCRATCH_BYTES // self.page_size
        self.maximum_page_count = self.connection.execute(
            f"PRAGMA max_page_count={requested_max_pages}"
        ).fetchone()[0]
        if self.maximum_page_count > requested_max_pages:
            raise ValueError("Places head SQLite page cap was not enforced")
        self.connection.execute(
            "CREATE TABLE token_counts (value TEXT PRIMARY KEY, records INTEGER NOT NULL)"
        )
        self.connection.execute(
            "CREATE TABLE prefix_counts (value TEXT PRIMARY KEY, records INTEGER NOT NULL)"
        )
        self.peak_scratch_bytes = 0
        self.peak_database_pages = 0
        self.peak_database_bytes = 0
        self.workspace_observer = workspace_observer
        self.observe_scratch()

    def scratch_bytes(self) -> int:
        return sum(
            path.stat().st_size
            for path in self.scratch_dir.rglob("*")
            if path.is_file()
        )

    def observe_scratch(self) -> int:
        physical = self.scratch_bytes()
        database_file_bytes = self.path.stat().st_size
        pages = self.connection.execute("PRAGMA page_count").fetchone()[0]
        database_bytes = pages * self.page_size
        current = physical - database_file_bytes + max(
            database_file_bytes, database_bytes
        )
        self.peak_scratch_bytes = max(self.peak_scratch_bytes, current)
        self.peak_database_pages = max(self.peak_database_pages, pages)
        self.peak_database_bytes = max(self.peak_database_bytes, database_bytes)
        if (
            current > MAX_HEAD_SCRATCH_BYTES
            or pages > self.maximum_page_count
            or database_bytes > MAX_HEAD_SCRATCH_BYTES
        ):
            raise ValueError(
                "Places head actual scratch usage exceeded its hard cap: "
                f"observed={current}, cap={MAX_HEAD_SCRATCH_BYTES}"
            )
        if self.workspace_observer is not None:
            self.workspace_observer(current)
        return current

    def add_counts(self, table: str, values: Counter[str]) -> None:
        if table not in {"token_counts", "prefix_counts"}:
            raise AssertionError("unknown Places head count table")
        try:
            self.connection.executemany(
                f"""
                INSERT INTO {table}(value, records) VALUES (?, ?)
                ON CONFLICT(value) DO UPDATE SET records = records + excluded.records
                """,
                sorted(values.items()),
            )
        except sqlite3.OperationalError as exc:
            raise ValueError(
                "Places head scratch database exceeded its hard cap"
            ) from exc
        self.observe_scratch()

    def admitted_values(
        self, table: str, minimum_candidates: int, maximum_values: int
    ) -> set[str]:
        if table not in {"token_counts", "prefix_counts"}:
            raise AssertionError("unknown Places head admission table")
        require_int(maximum_values, "Places admitted value cap")
        rows = self.connection.execute(
            f"SELECT value FROM {table} WHERE records >= ? ORDER BY value LIMIT ?",
            (minimum_candidates, maximum_values + 1),
        ).fetchall()
        if len(rows) > maximum_values:
            raise ValueError("Places admitted head keys exceed the Worker reader cap")
        self.observe_scratch()
        return {value for (value,) in rows}

    def count_rows(self) -> tuple[int, int]:
        tokens = self.connection.execute(
            "SELECT count(*) FROM token_counts"
        ).fetchone()[0]
        prefixes = self.connection.execute(
            "SELECT count(*) FROM prefix_counts"
        ).fetchone()[0]
        return tokens, prefixes

    def finish(self) -> None:
        self.connection.commit()
        self.observe_scratch()

    def close(self) -> None:
        self.connection.close()
        self.path.unlink(missing_ok=True)


class _ArtifactMaterializer:
    def __init__(
        self,
        *,
        artifact_root: Path,
        scratch_dir: Path,
        fetch_command: list[str] | None,
        observe_workspace: Callable[[int], None],
    ) -> None:
        self.artifact_root = artifact_root
        self.scratch_dir = scratch_dir
        self.fetch_command = fetch_command
        self.observe_workspace = observe_workspace
        self.fetched_fragments = 0
        self.fetched_bytes = 0
        self.peak_materialized_fragment_bytes = 0

    @staticmethod
    def _remove(path: Path) -> None:
        if path.is_dir():
            path.rmdir()
        else:
            path.unlink(missing_ok=True)

    @contextlib.contextmanager
    def path(self, fragment: dict[str, Any]) -> Iterator[Path]:
        local = safe_artifact_path(self.artifact_root, fragment["object_key"])
        temporary: Path | None = None
        if not local.is_file():
            if self.fetch_command is None:
                raise ValueError(
                    f"missing Places head fragment: {fragment['object_key']}"
                )
            self.observe_workspace(fragment["bytes"])
            self.scratch_dir.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(
                prefix="places-head-fragment-", suffix=".parquet", dir=self.scratch_dir
            )
            os.close(descriptor)
            temporary = Path(name)
            temporary.unlink()
            argv = [
                item.replace("{object_key}", fragment["object_key"]).replace(
                    "{output}", str(temporary)
                )
                for item in self.fetch_command
            ]
            try:
                subprocess.run(argv, check=True)
            except (OSError, subprocess.CalledProcessError) as exc:
                self._remove(temporary)
                raise ValueError("Places head fragment fetch adapter failed") from exc
            if not temporary.is_file():
                self._remove(temporary)
                raise ValueError("Places head fragment fetch adapter produced no file")
            local = temporary
            self.observe_workspace(local.stat().st_size)
        try:
            actual_sha256, actual_bytes = sha256_file(local)
            if (actual_bytes, actual_sha256) != (
                fragment["bytes"],
                fragment["sha256"],
            ):
                raise ValueError(
                    f"Places head fragment identity mismatch: {fragment['object_key']}"
                )
            if temporary is not None:
                self.fetched_fragments += 1
                self.fetched_bytes += actual_bytes
                self.peak_materialized_fragment_bytes = max(
                    self.peak_materialized_fragment_bytes, actual_bytes
                )
            yield local
        finally:
            if temporary is not None:
                self._remove(temporary)
                self.observe_workspace(0)


def _all_fragments(plan: dict[str, Any]) -> list[dict[str, Any]]:
    fragments = [
        fragment for job in plan["reduce_jobs"] for fragment in job["input_fragments"]
    ]
    return sorted(
        fragments,
        key=lambda item: (
            item["execution_group"],
            item["map_index"],
            item["minimum_sort_key"],
            item["sha256"],
        ),
    )


def _candidate_sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
    rank = round(row["confidence"] * 255)
    return (
        -rank,
        row["partition_key"],
        -rank,
        row["gers_id"],
        row["source_uri"],
        row["source_row_group"],
        row["source_row_index"],
    )


def _push_candidate(
    candidates: dict[str, list[tuple[tuple[Any, ...], bytes]]],
    key: str,
    sort_key: tuple[Any, ...],
    projection: bytes,
    limit: int,
) -> None:
    values = candidates.setdefault(key, [])
    item = (sort_key, projection)
    if len(values) < limit:
        values.append(item)
        return
    worst_index = max(range(len(values)), key=lambda index: values[index][0])
    if sort_key < values[worst_index][0]:
        values[worst_index] = item


def _trim_famous(
    values: list[tuple[tuple[Any, ...], Place, tuple[str, ...], int]], cap: int
) -> list[tuple[tuple[Any, ...], Place, tuple[str, ...], int]]:
    if cap <= 0:
        return []
    if len(values) > cap * 2:
        return sorted(values, key=lambda item: item[0])[:cap]
    return values


def _write_head_object(
    candidates: dict[str, list[tuple[tuple[Any, ...], bytes]]],
    output: Path,
    *,
    famous_cap: int,
    existing_scratch_bytes: int,
    durable_provenance: dict[str, Any],
) -> dict[str, Any]:
    preflight_entries_bytes = sum(
        len(encode_varint(len(projection))) + len(projection)
        for values in candidates.values()
        for _, projection in values
    )
    preflight_index_bytes = 10 + sum(
        len(key.encode("utf-8")) + 30 for key in candidates
    )
    projected_workspace = (
        existing_scratch_bytes
        + preflight_entries_bytes * 2
        + preflight_index_bytes
        + 64_000_000
    )
    if projected_workspace > MAX_HEAD_SCRATCH_BYTES:
        raise ValueError(
            "Places head conservative object bound exceeded its workspace cap"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, entries_name = tempfile.mkstemp(
        prefix="places-head-entries-", suffix=".bin", dir=output.parent
    )
    os.close(descriptor)
    entries_path = Path(entries_name)
    key_entries: list[tuple[str, int, int]] = []
    entry_sizes: list[int] = []
    family_key_counts = dict.fromkeys(HEAD_KEY_FAMILIES, 0)
    family_entry_bytes = dict.fromkeys(HEAD_KEY_FAMILIES, 0)
    cursor = 0
    try:
        with entries_path.open("wb") as entries_target:
            for key in sorted(candidates):
                ordered = sorted(candidates[key], key=lambda item: item[0])
                entry = b"".join(
                    encode_varint(len(projection)) + projection
                    for _, projection in ordered
                )
                if len(key.encode("utf-8")) > READER_MAX_KEY_BYTES:
                    raise ValueError("Places global head key exceeds the Worker cap")
                if len(entry) > READER_MAX_HEAD_ENTRY_BYTES:
                    raise ValueError("Places global head entry exceeds the Worker cap")
                key_entries.append((key, cursor, len(entry)))
                if (
                    existing_scratch_bytes + cursor + len(entry)
                    > MAX_HEAD_SCRATCH_BYTES
                ):
                    raise ValueError("Places head entries exceeded its workspace cap")
                entries_target.write(entry)
                cursor += len(entry)
                entry_sizes.append(len(entry))
                family = key.split(":", 1)[0]
                family_key_counts[family] += 1
                family_entry_bytes[family] += len(entry)
        peak_workspace_bytes = existing_scratch_bytes + entries_path.stat().st_size
        if peak_workspace_bytes > MAX_HEAD_SCRATCH_BYTES:
            raise ValueError("Places head entries exceeded its hard workspace cap")
        key_index = encode_key_index(key_entries)
        if (
            len(key_entries) > READER_MAX_HEAD_KEYS
            or len(key_index) > READER_MAX_HEAD_INDEX_BYTES
            or cursor > MAX_HEAD_ENTRIES_BYTES
        ):
            raise ValueError("Places global head exceeds a Worker reader cap")
        directory = {
            "schema_version": 1,
            "magic": MAGIC.decode(),
            "key_count": len(key_entries),
            "head_limit": 10,
            "provenance": durable_provenance,
            "components": {
                "key_index": {"length": len(key_index)},
                "entries": {"length": cursor},
            },
        }
        if famous_cap > 0:
            directory["head_famous_cap"] = famous_cap
            directory["e2_key_count"] = family_key_counts["e2"]
            directory["admission"] = HEAD_ADMISSION_MARKER
        for _ in range(8):
            directory_bytes = json.dumps(
                directory, sort_keys=True, separators=(",", ":")
            ).encode()
            offset = PREAMBLE.size + len(directory_bytes)
            changed = False
            for name, length in (("key_index", len(key_index)), ("entries", cursor)):
                if directory["components"][name].get("offset") != offset:
                    directory["components"][name]["offset"] = offset
                    changed = True
                offset += length
            if not changed:
                break
        else:
            raise RuntimeError("Places head directory offsets did not stabilize")
        directory_bytes = json.dumps(
            directory, sort_keys=True, separators=(",", ":")
        ).encode()
        with output.open("wb") as target:
            target.write(PREAMBLE.pack(MAGIC, len(directory_bytes)))
            target.write(directory_bytes)
            target.write(key_index)
            with entries_path.open("rb") as entries_source:
                shutil.copyfileobj(entries_source, target, length=1024 * 1024)
        peak_workspace_bytes = max(
            peak_workspace_bytes,
            existing_scratch_bytes
            + entries_path.stat().st_size
            + output.stat().st_size,
        )
        if peak_workspace_bytes > MAX_HEAD_SCRATCH_BYTES:
            raise ValueError("Places head object exceeded its hard workspace cap")
        distribution = {
            "count": len(entry_sizes),
            "min": min(entry_sizes, default=0),
            "max": max(entry_sizes, default=0),
            "mean": statistics.mean(entry_sizes) if entry_sizes else 0,
            "total": sum(entry_sizes),
        }
        return {
            "key_count": len(key_entries),
            "key_index_bytes": len(key_index),
            "entries_bytes": cursor,
            "key_counts_by_family": family_key_counts,
            "entry_bytes_by_family": family_entry_bytes,
            "entry_size_distribution": distribution,
            "_peak_workspace_bytes": peak_workspace_bytes,
        }
    finally:
        entries_path.unlink(missing_ok=True)


def build_global_head(
    request_value: Any,
    plan_value: Any,
    *,
    artifact_root: Path,
    scratch_dir: Path,
    output: Path,
    fragment_fetch_command: list[str] | None = None,
    fragment_reader: FragmentReader | None = None,
    runtime_provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if output.name != "head.phrp":
        raise ValueError("Places global head output must be named head.phrp")
    fragment_fetch_command = validate_fetch_command(fragment_fetch_command)
    request = global_v2_build_request.validate_request(request_value)
    plan = validate_places_plan(plan_value)
    if request_sha256(request) != plan["request"]["sha256"]:
        raise ValueError("Places head request differs from the executor plan")
    policy = request["families"]["places"]["global_head"]
    if (
        policy["format"] != MAGIC.decode()
        or policy["admission"] != HEAD_ADMISSION_MARKER
        or tuple(policy["prefix_policy"]["lengths"]) != HEAD_PREFIX_LENGTHS
        or policy["result_cap"] != 10
        or policy["famous_cap"] > READER_MAX_HEAD_KEYS
        or policy["provenance"]["predecessor_family_manifest_sha256"]
        != plan["partition"]["predecessor_family_manifest_sha256"]
        or policy["provenance"]["predecessor_family_manifest"]
        != plan["partition"]["predecessor_family_manifest"]
    ):
        raise ValueError(
            "Places head policy differs from the implemented frozen contract"
        )
    if fragment_reader is None:
        default_reader = PyArrowFragmentReader()
        fragment_reader = default_reader
    else:
        default_reader = None
        if runtime_provenance is None:
            raise ValueError("injected Places head reader requires runtime provenance")
    fragments = _all_fragments(plan)
    leaves = {leaf["cell"] for leaf in plan["leaves"]}
    minimum_level = plan["partition"]["minimum_level"]
    maximum_level = plan["partition"]["maximum_level"]
    active_materialized_fragment_bytes = 0
    peak_fragment_workspace_bytes = 0

    def observe_combined_workspace(scratch_bytes: int) -> None:
        nonlocal peak_fragment_workspace_bytes
        current = scratch_bytes + active_materialized_fragment_bytes
        peak_fragment_workspace_bytes = max(peak_fragment_workspace_bytes, current)
        if current > MAX_HEAD_SCRATCH_BYTES:
            raise ValueError("Places head fragment staging exceeded its workspace cap")

    counts = _CountDatabase(
        scratch_dir / "counts", workspace_observer=observe_combined_workspace
    )

    def observe_fragment_workspace(materialized_fragment_bytes: int) -> None:
        nonlocal active_materialized_fragment_bytes
        active_materialized_fragment_bytes = materialized_fragment_bytes
        counts.observe_scratch()

    materializer = _ArtifactMaterializer(
        artifact_root=artifact_root,
        scratch_dir=scratch_dir / "fragments",
        fetch_command=fragment_fetch_command,
        observe_workspace=observe_fragment_workspace,
    )
    famous: list[tuple[tuple[Any, ...], Place, tuple[str, ...], int]] = []
    first_pass_rows = 0
    try:
        token_batch: Counter[str] = Counter()
        prefix_batch: Counter[str] = Counter()
        token_batch_string_bytes = 0
        prefix_batch_string_bytes = 0
        peak_count_batch_string_bytes = 0
        famous_serialized_bytes = 0
        peak_famous_serialized_bytes = 0
        for fragment in fragments:
            fragment_rows = 0
            with materializer.path(fragment) as path:
                counts.observe_scratch()
                for raw in fragment_reader(fragment, path):
                    _, row = _validate_fragment_row(
                        raw,
                        fragment=fragment,
                        maximum_level=maximum_level,
                        leaves=leaves,
                        minimum_level=minimum_level,
                    )
                    place = place_from_row(row, first_pass_rows + 1)
                    terms = place_terms(place)
                    for token in terms:
                        token_bytes = len(token.encode("utf-8"))
                        if token_bytes + 2 > READER_MAX_KEY_BYTES:
                            raise ValueError(
                                "Places exact token exceeds the Worker key-byte cap"
                            )
                        if token not in token_batch:
                            token_batch_string_bytes += token_bytes
                        token_batch[token] += 1
                        for length in HEAD_PREFIX_LENGTHS:
                            if len(token) >= length:
                                prefix = token[:length]
                                if prefix not in prefix_batch:
                                    prefix_batch_string_bytes += len(
                                        prefix.encode("utf-8")
                                    )
                                prefix_batch[prefix] += 1
                    current_batch_string_bytes = (
                        token_batch_string_bytes + prefix_batch_string_bytes
                    )
                    peak_count_batch_string_bytes = max(
                        peak_count_batch_string_bytes, current_batch_string_bytes
                    )
                    if current_batch_string_bytes > MAX_HEAD_COUNT_BATCH_STRING_BYTES:
                        raise ValueError(
                            "Places head count batch exceeds its string-byte cap"
                        )
                    name_brand_tokens = famous_name_brand_tokens(place)
                    serialized_bytes = len(encode_record(place)) + sum(
                        len(value.encode("utf-8")) for value in name_brand_tokens
                    )
                    famous.append(
                        (
                            _candidate_sort_key(row),
                            place,
                            name_brand_tokens,
                            serialized_bytes,
                        )
                    )
                    famous_serialized_bytes += serialized_bytes
                    before_trim = len(famous)
                    famous = _trim_famous(famous, policy["famous_cap"])
                    if len(famous) != before_trim:
                        famous_serialized_bytes = sum(item[3] for item in famous)
                    peak_famous_serialized_bytes = max(
                        peak_famous_serialized_bytes, famous_serialized_bytes
                    )
                    if famous_serialized_bytes > MAX_HEAD_FAMOUS_SERIALIZED_BYTES:
                        raise ValueError(
                            "Places head famous set exceeds its serialized-byte cap"
                        )
                    fragment_rows += 1
                    first_pass_rows += 1
                    if (
                        len(token_batch) + len(prefix_batch)
                        >= MAX_HEAD_COUNT_BATCH_KEYS
                    ):
                        counts.add_counts("token_counts", token_batch)
                        counts.add_counts("prefix_counts", prefix_batch)
                        token_batch.clear()
                        prefix_batch.clear()
                        token_batch_string_bytes = 0
                        prefix_batch_string_bytes = 0
            if fragment_rows != fragment["records"]:
                raise ValueError(
                    "Places head first-pass fragment rows do not reconcile"
                )
        counts.add_counts("token_counts", token_batch)
        counts.add_counts("prefix_counts", prefix_batch)
        counts.finish()
        famous = sorted(famous, key=lambda item: item[0])[: policy["famous_cap"]]
        admitted_tokens = counts.admitted_values(
            "token_counts", policy["minimum_candidates"], READER_MAX_HEAD_KEYS
        )
        pair_tokens: dict[str, tuple[str, str]] = {}
        for _, _, name_brand_tokens, _ in famous:
            admitted_tokens.update(name_brand_tokens)
            if len(admitted_tokens) > READER_MAX_HEAD_KEYS:
                raise ValueError(
                    "Places famous exact keys exceed the Worker reader cap"
                )
            bounded = name_brand_tokens[:FAMOUS_PAIR_TOKEN_LIMIT]
            for first_index in range(len(bounded)):
                for second_index in range(first_index + 1, len(bounded)):
                    low, high = sorted((bounded[first_index], bounded[second_index]))
                    pair_tokens[famous_pair_token_key(low, high)] = (low, high)
                    if len(admitted_tokens) + len(pair_tokens) > READER_MAX_HEAD_KEYS:
                        raise ValueError(
                            "Places famous pair keys exceed the Worker reader cap"
                        )
        remaining_prefix_keys = (
            READER_MAX_HEAD_KEYS - len(admitted_tokens) - len(pair_tokens)
        )
        admitted_prefixes = counts.admitted_values(
            "prefix_counts", policy["minimum_candidates"], remaining_prefix_keys
        )
        total_keys = len(admitted_tokens) + len(admitted_prefixes) + len(pair_tokens)
        if total_keys > READER_MAX_HEAD_KEYS:
            raise ValueError("Places admitted head keys exceed the Worker reader cap")
        if total_keys * policy["result_cap"] > MAX_HEAD_CANDIDATE_SLOTS:
            raise ValueError("Places global head exceeds its candidate-slot memory cap")
        pair_by_token: dict[str, list[tuple[str, str]]] = {}
        for key, (low, high) in pair_tokens.items():
            pair_by_token.setdefault(low, []).append((key, high))
            pair_by_token.setdefault(high, []).append((key, low))
        admitted_key_bytes = sum(
            len(value.encode("utf-8")) + 2 for value in admitted_tokens
        ) + sum(len(value.encode("utf-8")) + 2 for value in admitted_prefixes)
        admitted_key_bytes += sum(len(value.encode("utf-8")) for value in pair_tokens)
        if admitted_key_bytes > MAX_HEAD_ADMITTED_KEY_BYTES:
            raise ValueError("Places admitted head keys exceed their string-byte cap")
        candidates: dict[str, list[tuple[tuple[Any, ...], bytes]]] = {}
        second_pass_rows = 0
        for fragment in fragments:
            fragment_rows = 0
            with materializer.path(fragment) as path:
                counts.observe_scratch()
                for raw in fragment_reader(fragment, path):
                    _, row = _validate_fragment_row(
                        raw,
                        fragment=fragment,
                        maximum_level=maximum_level,
                        leaves=leaves,
                        minimum_level=minimum_level,
                    )
                    place = place_from_row(row, second_pass_rows + 1)
                    terms = place_terms(place)
                    sort_key = _candidate_sort_key(row)
                    projection = encode_record(place)
                    for token in terms & admitted_tokens:
                        _push_candidate(
                            candidates,
                            f"e:{token}",
                            sort_key,
                            projection,
                            policy["result_cap"],
                        )
                    prefixes = {
                        token[:length]
                        for token in terms
                        for length in HEAD_PREFIX_LENGTHS
                        if len(token) >= length and token[:length] in admitted_prefixes
                    }
                    for prefix in prefixes:
                        _push_candidate(
                            candidates,
                            f"p:{prefix}",
                            sort_key,
                            projection,
                            policy["result_cap"],
                        )
                    matched_pairs: set[str] = set()
                    for token in terms:
                        for key, other in pair_by_token.get(token, ()):
                            if other in terms:
                                matched_pairs.add(key)
                    for key in matched_pairs:
                        _push_candidate(
                            candidates,
                            key,
                            sort_key,
                            projection,
                            policy["result_cap"],
                        )
                    fragment_rows += 1
                    second_pass_rows += 1
            if fragment_rows != fragment["records"]:
                raise ValueError(
                    "Places head second-pass fragment rows do not reconcile"
                )
        if (
            first_pass_rows != plan["totals"]["retained_records"]
            or second_pass_rows != first_pass_rows
        ):
            raise ValueError("Places head passes do not reconcile to retained map rows")
        exact_smoke_keys = sorted(key for key in candidates if key.startswith("e:"))
        if not exact_smoke_keys:
            raise ValueError("Places global head has no deterministic exact smoke key")
        smoke_key = exact_smoke_keys[0]
        smoke_projection = sorted(candidates[smoke_key], key=lambda item: item[0])[0][1]
        smoke_place = decode_record(smoke_projection)
        smoke_sample = {
            "query": smoke_key.removeprefix("e:"),
            "expected_id": smoke_place["id"],
            "types": ["poi"],
            "source": "built-global-head-exact-key",
        }
        # Pair keys with no shared document are intentionally absent, matching
        # the existing head producer. Every admitted exact/prefix key has at
        # least one candidate by construction.
        object_report = _write_head_object(
            candidates,
            output,
            famous_cap=policy["famous_cap"],
            existing_scratch_bytes=counts.observe_scratch(),
            durable_provenance={
                "request_sha256": plan["request"]["sha256"],
                "plan_sha256": plan["plan_sha256"],
                "head_policy_sha256": digest_value(policy),
                "lineage_generation": plan["partition"]["lineage_generation"],
                "predecessor_family_manifest_sha256": plan["partition"][
                    "predecessor_family_manifest_sha256"
                ],
                "predecessor_family_manifest": plan["partition"][
                    "predecessor_family_manifest"
                ],
            },
        )
        peak_object_workspace_bytes = object_report.pop("_peak_workspace_bytes")
        artifact_sha256, artifact_bytes = sha256_file(output)
        token_universe, prefix_universe = counts.count_rows()
        if default_reader is not None:
            reader_provenance = default_reader.provenance()
        else:
            reader_provenance = runtime_provenance
        without_digest = {
            "schema": HEAD_REPORT_SCHEMA,
            "version": HEAD_VERSION,
            "plan_sha256": plan["plan_sha256"],
            "request_sha256": plan["request"]["sha256"],
            "status": "complete",
            "policy": policy,
            "lineage_generation": plan["partition"]["lineage_generation"],
            "predecessor_family_manifest_sha256": plan["partition"][
                "predecessor_family_manifest_sha256"
            ],
            "predecessor_family_manifest": plan["partition"][
                "predecessor_family_manifest"
            ],
            "runtime": {
                "required_python_version": REQUIRED_PYTHON_VERSION,
                "required_pyarrow_version": REQUIRED_PYARROW_VERSION,
                "actual_python_version": platform.python_version(),
                "fragment_reader": reader_provenance,
                "head_writer_module_sha256": _module_sha256("global_v2_places_head.py"),
                "tokenizer_module_sha256": _module_sha256(
                    "experiment_places_compact_index.py"
                ),
            },
            "fragment_materialization": {
                "adapter": "local-or-no-shell-argv-v1",
                "remote_fetch_enabled": fragment_fetch_command is not None,
                "fetch_passes": 2,
                "fetched_fragments": materializer.fetched_fragments,
                "fetched_bytes": materializer.fetched_bytes,
                "maximum_simultaneously_materialized_fragments": 1,
                "peak_materialized_fragment_bytes": (
                    materializer.peak_materialized_fragment_bytes
                ),
                "identity_verification": "exact-plan-bytes-and-sha256",
            },
            "bounds": {
                "passes": 2,
                "maximum_open_fragment_files": HEAD_MAX_OPEN_FRAGMENT_FILES,
                "maximum_scratch_bytes": MAX_HEAD_SCRATCH_BYTES,
                "maximum_count_batch_keys": MAX_HEAD_COUNT_BATCH_KEYS,
                "maximum_count_batch_string_bytes": MAX_HEAD_COUNT_BATCH_STRING_BYTES,
                "maximum_famous_serialized_bytes": MAX_HEAD_FAMOUS_SERIALIZED_BYTES,
                "maximum_admitted_key_bytes": MAX_HEAD_ADMITTED_KEY_BYTES,
                "maximum_candidate_slots": MAX_HEAD_CANDIDATE_SLOTS,
                "admitted_key_cap": READER_MAX_HEAD_KEYS,
                "result_cap": policy["result_cap"],
            },
            "usage": {
                "peak_scratch_bytes": counts.peak_scratch_bytes,
                "sqlite_page_size": counts.page_size,
                "sqlite_maximum_page_count": counts.maximum_page_count,
                "peak_sqlite_page_count": counts.peak_database_pages,
                "peak_sqlite_database_bytes": counts.peak_database_bytes,
                "peak_count_batch_string_bytes": peak_count_batch_string_bytes,
                "peak_famous_serialized_bytes": peak_famous_serialized_bytes,
                "admitted_key_bytes": admitted_key_bytes,
                "peak_workspace_bytes": max(
                    counts.peak_scratch_bytes,
                    peak_fragment_workspace_bytes,
                    peak_object_workspace_bytes,
                ),
            },
            "accounting": {
                "retained_records": plan["totals"]["retained_records"],
                "first_pass_records": first_pass_rows,
                "second_pass_records": second_pass_rows,
                "source_fragments": len(fragments),
                "token_universe": token_universe,
                "prefix_universe": prefix_universe,
                "admitted_exact_keys": len(admitted_tokens),
                "admitted_prefix_keys": len(admitted_prefixes),
                "admitted_pair_keys": len(pair_tokens),
                "emitted_keys": object_report["key_count"],
                "candidate_slots": sum(len(items) for items in candidates.values()),
            },
            "object": object_report,
            "artifact": {
                "object": output.name,
                "bytes": artifact_bytes,
                "sha256": artifact_sha256,
                "format": MAGIC.decode(),
            },
            "smoke_sample": smoke_sample,
        }
        return {
            **without_digest,
            "report_sha256": digest_value(without_digest),
        }
    finally:
        counts.close()


def validate_head_report(
    value: Any, request_value: Any, plan_value: Any
) -> dict[str, Any]:
    request = global_v2_build_request.validate_request(request_value)
    plan = validate_places_plan(plan_value)
    report = require_exact(
        value,
        {
            "schema",
            "version",
            "plan_sha256",
            "request_sha256",
            "status",
            "policy",
            "lineage_generation",
            "predecessor_family_manifest_sha256",
            "predecessor_family_manifest",
            "runtime",
            "fragment_materialization",
            "bounds",
            "usage",
            "accounting",
            "object",
            "artifact",
            "smoke_sample",
            "report_sha256",
        },
        "Places head report",
    )
    if report["schema"] != HEAD_REPORT_SCHEMA or report["version"] != HEAD_VERSION:
        raise ValueError("Places head report schema/version is invalid")
    require_sha256(report["report_sha256"], "Places head report_sha256")
    without_digest = {
        key: item for key, item in report.items() if key != "report_sha256"
    }
    if digest_value(without_digest) != report["report_sha256"]:
        raise ValueError("Places head report digest differs from its contents")
    if (
        report["plan_sha256"] != plan["plan_sha256"]
        or report["request_sha256"] != request_sha256(request)
        or report["status"] != "complete"
        or report["policy"] != request["families"]["places"]["global_head"]
        or report["lineage_generation"] != plan["partition"]["lineage_generation"]
        or report["predecessor_family_manifest_sha256"]
        != plan["partition"]["predecessor_family_manifest_sha256"]
        or report["predecessor_family_manifest"]
        != plan["partition"]["predecessor_family_manifest"]
    ):
        raise ValueError("Places head report provenance differs from request/plan")
    smoke_sample = require_exact(
        report["smoke_sample"],
        {"query", "expected_id", "types", "source"},
        "Places head smoke sample",
    )
    if (
        not isinstance(smoke_sample["query"], str)
        or not smoke_sample["query"]
        or not isinstance(smoke_sample["expected_id"], str)
        or not smoke_sample["expected_id"]
        or smoke_sample["types"] != ["poi"]
        or smoke_sample["source"] != "built-global-head-exact-key"
    ):
        raise ValueError("Places head smoke sample is invalid")
    runtime = report["runtime"]
    if (
        not isinstance(runtime, dict)
        or runtime.get("required_python_version") != REQUIRED_PYTHON_VERSION
        or runtime.get("required_pyarrow_version") != REQUIRED_PYARROW_VERSION
        or not isinstance(runtime.get("actual_python_version"), str)
        or not runtime["actual_python_version"]
        or not isinstance(runtime.get("fragment_reader"), dict)
        or not runtime["fragment_reader"]
        or not isinstance(runtime.get("head_writer_module_sha256"), str)
        or not isinstance(runtime.get("tokenizer_module_sha256"), str)
    ):
        raise ValueError("Places head runtime provenance is required")
    require_sha256(
        runtime["head_writer_module_sha256"], "Places head writer module sha256"
    )
    require_sha256(runtime["tokenizer_module_sha256"], "Places tokenizer module sha256")
    materialization = require_exact(
        report["fragment_materialization"],
        {
            "adapter",
            "remote_fetch_enabled",
            "fetch_passes",
            "fetched_fragments",
            "fetched_bytes",
            "maximum_simultaneously_materialized_fragments",
            "peak_materialized_fragment_bytes",
            "identity_verification",
        },
        "Places head fragment materialization",
    )
    fetched_fragments = require_int(
        materialization["fetched_fragments"], "Places head fetched fragments"
    )
    fetched_bytes = require_int(
        materialization["fetched_bytes"], "Places head fetched bytes"
    )
    peak_materialized = require_int(
        materialization["peak_materialized_fragment_bytes"],
        "Places head peak materialized fragment bytes",
    )
    planned_fragments = [
        fragment for job in plan["reduce_jobs"] for fragment in job["input_fragments"]
    ]
    if (
        materialization["adapter"] != "local-or-no-shell-argv-v1"
        or type(materialization["remote_fetch_enabled"]) is not bool
        or materialization["fetch_passes"] != 2
        or materialization["maximum_simultaneously_materialized_fragments"] != 1
        or materialization["identity_verification"] != "exact-plan-bytes-and-sha256"
        or (
            materialization["remote_fetch_enabled"]
            and (
                fetched_fragments != len(planned_fragments) * 2
                or fetched_bytes != sum(item["bytes"] for item in planned_fragments) * 2
                or peak_materialized != max(item["bytes"] for item in planned_fragments)
            )
        )
        or (
            not materialization["remote_fetch_enabled"]
            and (fetched_fragments != 0 or fetched_bytes != 0 or peak_materialized != 0)
        )
    ):
        raise ValueError("Places head fragment materialization evidence is invalid")
    bounds = report["bounds"]
    if bounds != {
        "passes": 2,
        "maximum_open_fragment_files": HEAD_MAX_OPEN_FRAGMENT_FILES,
        "maximum_scratch_bytes": MAX_HEAD_SCRATCH_BYTES,
        "maximum_count_batch_keys": MAX_HEAD_COUNT_BATCH_KEYS,
        "maximum_count_batch_string_bytes": MAX_HEAD_COUNT_BATCH_STRING_BYTES,
        "maximum_famous_serialized_bytes": MAX_HEAD_FAMOUS_SERIALIZED_BYTES,
        "maximum_admitted_key_bytes": MAX_HEAD_ADMITTED_KEY_BYTES,
        "maximum_candidate_slots": MAX_HEAD_CANDIDATE_SLOTS,
        "admitted_key_cap": READER_MAX_HEAD_KEYS,
        "result_cap": request["families"]["places"]["global_head"]["result_cap"],
    }:
        raise ValueError("Places head bounds differ from the implemented contract")
    usage = report["usage"]
    if (
        not isinstance(usage, dict)
        or set(usage)
        != {
            "peak_scratch_bytes",
            "sqlite_page_size",
            "sqlite_maximum_page_count",
            "peak_sqlite_page_count",
            "peak_sqlite_database_bytes",
            "peak_count_batch_string_bytes",
            "peak_famous_serialized_bytes",
            "admitted_key_bytes",
            "peak_workspace_bytes",
        }
        or type(usage["peak_scratch_bytes"]) is not int
        or not 0 <= usage["peak_scratch_bytes"] <= MAX_HEAD_SCRATCH_BYTES
        or type(usage["peak_workspace_bytes"]) is not int
        or not usage["peak_scratch_bytes"]
        <= usage["peak_workspace_bytes"]
        <= MAX_HEAD_SCRATCH_BYTES
        or type(usage["sqlite_page_size"]) is not int
        or usage["sqlite_page_size"] < 512
        or type(usage["sqlite_maximum_page_count"]) is not int
        or usage["sqlite_maximum_page_count"]
        > MAX_HEAD_SCRATCH_BYTES // usage["sqlite_page_size"]
        or type(usage["peak_sqlite_page_count"]) is not int
        or not 0
        <= usage["peak_sqlite_page_count"]
        <= usage["sqlite_maximum_page_count"]
        or type(usage["peak_sqlite_database_bytes"]) is not int
        or usage["peak_sqlite_database_bytes"]
        != usage["peak_sqlite_page_count"] * usage["sqlite_page_size"]
        or usage["peak_sqlite_database_bytes"] > MAX_HEAD_SCRATCH_BYTES
        or type(usage["peak_count_batch_string_bytes"]) is not int
        or not 0
        <= usage["peak_count_batch_string_bytes"]
        <= MAX_HEAD_COUNT_BATCH_STRING_BYTES
        or type(usage["peak_famous_serialized_bytes"]) is not int
        or not 0
        <= usage["peak_famous_serialized_bytes"]
        <= MAX_HEAD_FAMOUS_SERIALIZED_BYTES
        or type(usage["admitted_key_bytes"]) is not int
        or not 0 <= usage["admitted_key_bytes"] <= MAX_HEAD_ADMITTED_KEY_BYTES
    ):
        raise ValueError("Places head observed disk usage is invalid")
    accounting = report["accounting"]
    retained = plan["totals"]["retained_records"]
    if (
        accounting.get("retained_records") != retained
        or accounting.get("first_pass_records") != retained
        or accounting.get("second_pass_records") != retained
        or accounting.get("source_fragments") != plan["totals"]["input_fragments"]
        or accounting.get("candidate_slots", MAX_HEAD_CANDIDATE_SLOTS + 1)
        > MAX_HEAD_CANDIDATE_SLOTS
        or accounting.get("emitted_keys") != report["object"].get("key_count")
    ):
        raise ValueError("Places head accounting does not reconcile")
    artifact = require_exact(
        report["artifact"],
        {"object", "bytes", "sha256", "format"},
        "Places head artifact",
    )
    if artifact["object"] != "head.phrp" or artifact["format"] != MAGIC.decode():
        raise ValueError("Places head artifact identity/format is invalid")
    require_int(artifact["bytes"], "Places head bytes", minimum=1)
    require_sha256(artifact["sha256"], "Places head sha256")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--artifacts-root", type=Path, required=True)
    parser.add_argument("--fragment-fetch-command-json")
    parser.add_argument("--scratch-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = build_global_head(
        json.loads(args.request.read_text()),
        json.loads(args.plan.read_text()),
        artifact_root=args.artifacts_root,
        scratch_dir=args.scratch_dir,
        output=args.output,
        fragment_fetch_command=parse_fetch_command(args.fragment_fetch_command_json),
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "report_sha256": report["report_sha256"],
                "records": report["accounting"]["retained_records"],
                "keys": report["accounting"]["emitted_keys"],
                "bytes": report["artifact"]["bytes"],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
