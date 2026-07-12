#!/usr/bin/env python3
"""Bounded scale gates for compact ID locator format v3.

Reads only public Overture data and writes local temporary artifacts. It never
reads or writes production geocoder shards. Remote scans have wall-clock,
memory, spill, output-row, and output-byte guards; S3 bytes and requests are
not exposed by DuckDB and are explicitly reported as unmetered.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import resource
import statistics
import struct
import sys
import threading
import time
from pathlib import Path
from typing import Any

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_id_index as build  # noqa: E402


FOOTER_SUFFIX_BYTES = 32 * 1024
DEFAULT_RELEASE = "2026-06-17.0"
DEFAULT_PREFIXES = ("0a1", "7f2", "e3c")
REPORT_VERSION = 1


def sql_literal(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def directory_bytes(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def rss_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == "darwin" else value * 1024)


class QueryGuard:
    """Interrupt one query and observe local spill/process high-water RSS."""

    def __init__(self, connection: Any, temp_dir: Path, seconds: int) -> None:
        if seconds <= 0:
            raise ValueError("deadline must be positive")
        self.connection = connection
        self.temp_dir = temp_dir
        self.seconds = seconds
        self.interrupted = threading.Event()
        self.done = threading.Event()
        self.start_rss = rss_bytes()
        self.max_rss = self.start_rss
        self.max_temp = directory_bytes(temp_dir)
        self.timer = threading.Timer(seconds, self._interrupt)
        self.monitor = threading.Thread(target=self._monitor, daemon=True)

    def _interrupt(self) -> None:
        self.interrupted.set()
        self.connection.interrupt()

    def _monitor(self) -> None:
        while not self.done.wait(0.2):
            self.max_rss = max(self.max_rss, rss_bytes())
            self.max_temp = max(self.max_temp, directory_bytes(self.temp_dir))

    def __enter__(self) -> "QueryGuard":
        self.timer.start()
        self.monitor.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self.timer.cancel()
        self.done.set()
        self.monitor.join(timeout=1)
        self.max_rss = max(self.max_rss, rss_bytes())
        self.max_temp = max(self.max_temp, directory_bytes(self.temp_dir))

    def metrics(self, elapsed: float) -> dict[str, Any]:
        return {
            "elapsed_seconds": round(elapsed, 3),
            "deadline_seconds": self.seconds,
            "deadline_interrupted": self.interrupted.is_set(),
            "max_observed_temp_bytes": self.max_temp,
            "rss_high_water_start_bytes": self.start_rss,
            "rss_high_water_max_bytes": self.max_rss,
            "rss_high_water_delta_bytes": max(0, self.max_rss - self.start_rss),
            "warning": "RSS is process high-water telemetry; S3 bytes/requests are unmetered.",
        }


def configure_remote(
    connection: Any, temp_dir: Path, memory_limit: str, temp_cap: int
) -> None:
    temp_dir.mkdir(parents=True, exist_ok=True)
    connection.execute("INSTALL httpfs; LOAD httpfs")
    connection.execute("SET s3_region='us-west-2'")
    connection.execute("SET threads=2")
    connection.execute("SET preserve_insertion_order=false")
    connection.execute(f"SET memory_limit={sql_literal(memory_limit)}")
    connection.execute(f"SET temp_directory={sql_literal(temp_dir)}")
    connection.execute(f"SET max_temp_directory_size='{int(temp_cap)}B'")
    connection.execute("SET http_timeout=120000")
    connection.execute("SET http_retries=2")


def next_prefix(prefix: str) -> str | None:
    value = int(prefix, 16)
    return (
        None
        if value == 16 ** len(prefix) - 1
        else format(value + 1, f"0{len(prefix)}x")
    )


def prefix_filter(prefix: str) -> str:
    upper = next_prefix(prefix)
    return f"id >= '{prefix}'" + (f" AND id < '{upper}'" if upper else "")


def extract_registry_prefix(
    connection: Any,
    prefix: str,
    output: Path,
    row_cap: int,
    byte_cap: int,
    deadline: int,
    temp_dir: Path,
) -> dict[str, Any]:
    if len(prefix) != 3 or any(char not in "0123456789abcdef" for char in prefix):
        raise ValueError(f"invalid prefix {prefix!r}")
    output.unlink(missing_ok=True)
    query = build._registry_id_query(3, prefix_filter(prefix))
    guard = QueryGuard(connection, temp_dir, deadline)
    started = time.monotonic()
    try:
        with guard:
            connection.execute(f"""
                COPY (SELECT * FROM ({query}) LIMIT {row_cap + 1})
                TO {sql_literal(output)} (FORMAT PARQUET, COMPRESSION ZSTD)
            """)
    except duckdb.Error as error:
        output.unlink(missing_ok=True)
        if guard.interrupted.is_set():
            raise RuntimeError(
                f"prefix {prefix} exceeded {deadline}s deadline"
            ) from error
        raise
    rows = int(
        connection.execute(
            "SELECT COUNT(*) FROM read_parquet(?)", [str(output)]
        ).fetchone()[0]
    )
    if rows > row_cap:
        output.unlink(missing_ok=True)
        raise RuntimeError(f"prefix {prefix} exceeded {row_cap:,}-row cap")
    size = output.stat().st_size
    if size > byte_cap:
        output.unlink(missing_ok=True)
        raise RuntimeError(f"prefix {prefix} exceeded {byte_cap:,}-byte cap")
    return {
        "prefix": prefix,
        "rows": rows,
        "materialized_bytes": size,
        "query_sha256": hashlib.sha256(query.encode()).hexdigest(),
        **guard.metrics(time.monotonic() - started),
    }


def discover_release_file_tuples(
    connection: Any, release: str, deadline: int, temp_dir: Path
) -> tuple[list[tuple[str, str, str]], dict[str, Any]]:
    """Inventory current-release objects without reading feature rows."""
    pattern = (
        f"s3://overturemaps-us-west-2/release/{release}/theme=*/type=*/**/*.parquet"
    )
    guard = QueryGuard(connection, temp_dir, deadline)
    started = time.monotonic()
    try:
        with guard:
            paths = [
                row[0]
                for row in connection.execute(
                    "SELECT file FROM glob(?)", [pattern]
                ).fetchall()
            ]
    except duckdb.Error as error:
        if guard.interrupted.is_set():
            raise RuntimeError("release file inventory deadline exceeded") from error
        raise
    tuples: set[tuple[str, str, str]] = set()
    for path in paths:
        parts = str(path).split("/")
        theme = next((part[6:] for part in parts if part.startswith("theme=")), "")
        feature_type = next(
            (part[5:] for part in parts if part.startswith("type=")), ""
        )
        filename = parts[-1]
        if build.TYPE_THEME_MAP.get(feature_type) != theme:
            raise RuntimeError(f"unmapped release path {path}")
        if not build._validate_source_filename(filename):
            raise RuntimeError(f"invalid release filename {path}")
        tuples.add((theme, feature_type, filename))
    if not tuples:
        raise RuntimeError("release inventory returned no files")
    return sorted(tuples), {
        "method": "S3 object glob; no feature-row DISTINCT",
        "object_paths": len(paths),
        "distinct_source_tuples": len(tuples),
        "pattern": pattern,
        **guard.metrics(time.monotonic() - started),
    }


def discover_historical_releases(
    connection: Any, deadline: int, result_cap: int, temp_dir: Path
) -> tuple[list[str], dict[str, Any]]:
    if not 1 <= result_cap <= 65_535:
        raise ValueError("historical result cap must be in 1..65535")
    query = f"""
        SELECT DISTINCT last_seen::VARCHAR AS last_seen_release
        FROM read_parquet('{build.REGISTRY_S3}*')
        WHERE path IS NULL AND last_seen IS NOT NULL
        ORDER BY last_seen_release LIMIT {result_cap + 1}
    """
    guard = QueryGuard(connection, temp_dir, deadline)
    started = time.monotonic()
    try:
        with guard:
            rows = connection.execute(query).fetchall()
    except duckdb.Error as error:
        if guard.interrupted.is_set():
            raise RuntimeError("historical DISTINCT deadline exceeded") from error
        raise
    releases = [str(row[0]) for row in rows]
    if len(releases) > result_cap:
        raise RuntimeError("historical release result cap exceeded")
    return releases, {
        "method": "exact global DISTINCT over path-null registry rows",
        "distinct_releases": len(releases),
        "query_sha256": hashlib.sha256(query.encode()).hexdigest(),
        **guard.metrics(time.monotonic() - started),
    }


def dictionary_from_prefixes(
    connection: Any, paths: list[Path], release: str
) -> dict[str, Any]:
    path_sql = ",".join(sql_literal(path) for path in paths)
    rows = connection.execute(f"""
        SELECT DISTINCT source_theme, feature_type, filename, last_seen_release
        FROM read_parquet([{path_sql}], union_by_name=true) ORDER BY ALL
    """).fetchall()
    source_files = [(a, b, c) for a, b, c, _ in rows if c is not None]
    releases = [d for _, _, c, d in rows if c is None and d is not None]
    return build._make_locator_dictionary(source_files, releases, release)


def write_compact_prefix(
    connection: Any,
    source: Path,
    output: Path,
    dictionary: dict[str, Any],
    row_group_size: int,
) -> dict[str, Any]:
    connection.execute("""CREATE OR REPLACE TEMP TABLE source_dictionary(
        source_file_id INTEGER, source_theme VARCHAR, feature_type VARCHAR, filename VARCHAR)""")
    source_rows = [
        (i, x["theme"], x["feature_type"], x["filename"])
        for i, x in enumerate(dictionary["source_files"], 1)
    ]
    if source_rows:
        connection.executemany(
            "INSERT INTO source_dictionary VALUES (?, ?, ?, ?)", source_rows
        )
    connection.execute("""CREATE OR REPLACE TEMP TABLE release_dictionary(
        last_seen_release_id INTEGER, last_seen_release VARCHAR)""")
    release_rows = [(i, x) for i, x in enumerate(dictionary["last_seen_releases"], 1)]
    if release_rows:
        connection.executemany(
            "INSERT INTO release_dictionary VALUES (?, ?)", release_rows
        )
    output.unlink(missing_ok=True)
    started = time.monotonic()
    connection.execute(f"""
        COPY (
          SELECT p.id, p.bbox_xmin, p.bbox_ymin, p.bbox_xmax, p.bbox_ymax,
                 s.source_file_id::INTEGER,
                 CASE WHEN p.filename IS NULL THEN r.last_seen_release_id::INTEGER END,
                 p.registry_member
          FROM read_parquet({sql_literal(source)}) p
          LEFT JOIN source_dictionary s USING (source_theme, feature_type, filename)
          LEFT JOIN release_dictionary r
            ON p.filename IS NULL AND p.last_seen_release = r.last_seen_release
          WHERE (s.source_file_id IS NOT NULL) != (r.last_seen_release_id IS NOT NULL)
          ORDER BY p.id
        ) TO {sql_literal(output)}
        (FORMAT PARQUET, COMPRESSION UNCOMPRESSED, ROW_GROUP_SIZE {int(row_group_size)})
    """)
    input_rows = int(
        connection.execute(
            "SELECT COUNT(*) FROM read_parquet(?)", [str(source)]
        ).fetchone()[0]
    )
    output_rows = int(
        connection.execute(
            "SELECT COUNT(*) FROM read_parquet(?)", [str(output)]
        ).fetchone()[0]
    )
    if input_rows != output_rows:
        output.unlink(missing_ok=True)
        raise RuntimeError(
            f"compact mapping dropped rows: {input_rows} != {output_rows}"
        )
    return {
        "rows": output_rows,
        "bytes": output.stat().st_size,
        "build_seconds": round(time.monotonic() - started, 3),
    }


def write_v1_prefix(
    connection: Any, source: Path, output: Path, row_group_size: int
) -> dict[str, Any]:
    """Write the exact legacy payload shape for a storage comparator."""
    output.unlink(missing_ok=True)
    started = time.monotonic()
    connection.execute(f"""
        COPY (
          SELECT id, bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax
          FROM read_parquet({sql_literal(source)}) ORDER BY id
        ) TO {sql_literal(output)}
        (FORMAT PARQUET, COMPRESSION UNCOMPRESSED,
         ROW_GROUP_SIZE {int(row_group_size)})
    """)
    rows = int(
        connection.execute(
            "SELECT COUNT(*) FROM read_parquet(?)", [str(output)]
        ).fetchone()[0]
    )
    return {
        "v1_bytes": output.stat().st_size,
        "v1_build_seconds": round(time.monotonic() - started, 3),
        "v1_rows": rows,
    }


def parquet_footer_size(path: Path) -> int:
    with path.open("rb") as handle:
        handle.seek(-8, os.SEEK_END)
        tail = handle.read(8)
    if len(tail) != 8 or tail[-4:] != b"PAR1":
        raise ValueError("not a complete Parquet file")
    return struct.unpack("<I", tail[:4])[0] + 8


def footer_read_plan(
    file_size: int, footer_size: int, suffix: int = FOOTER_SUFFIX_BYTES
) -> dict[str, Any]:
    if file_size < 8 or not 8 <= footer_size <= file_size or suffix <= 0:
        raise ValueError("invalid footer plan inputs")
    initial = min(file_size, suffix)
    retry = footer_size > initial
    return {
        "initial_suffix_requested_bytes": suffix,
        "initial_suffix_returned_bytes": initial,
        "footer_retry_required": retry,
        "footer_retry_requested_bytes": footer_size if retry else 0,
        "footer_range_reads": 2 if retry else 1,
        "total_footer_range_bytes": initial + (footer_size if retry else 0),
    }


def row_group_spans(connection: Any, path: Path) -> list[int]:
    rows = connection.execute(
        """
        SELECT row_group_id,
          MIN(LEAST(COALESCE(dictionary_page_offset,data_page_offset),data_page_offset)),
          MAX(LEAST(COALESCE(dictionary_page_offset,data_page_offset),data_page_offset)
              + total_compressed_size)
        FROM parquet_metadata(?) GROUP BY row_group_id ORDER BY row_group_id
    """,
        [str(path)],
    ).fetchall()
    return [int(end - start) for _, start, end in rows]


def summarize_compact_prefix(connection: Any, path: Path) -> dict[str, Any]:
    size = path.stat().st_size
    footer = parquet_footer_size(path)
    plan = footer_read_plan(size, footer)
    spans = row_group_spans(connection, path)
    cold = [span + plan["total_footer_range_bytes"] for span in spans]
    return {
        "file_bytes": size,
        "footer_bytes": footer,
        "row_groups": len(spans),
        "row_group_range_bytes": {
            "min": min(spans),
            "p50": int(statistics.median(spans)),
            "max": max(spans),
        },
        "cold_lookup_range_bytes": {
            "min": min(cold),
            "p50": int(statistics.median(cold)),
            "max": max(cold),
        },
        "cached_footer_lookup_range_bytes_p50": int(statistics.median(spans)),
        **plan,
    }


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1)]


def benchmark_dictionary_cold_cached(
    dictionary: dict[str, Any], iterations: int
) -> dict[str, Any]:
    if iterations < 2:
        raise ValueError("iterations must be at least two")
    raw = build._canonical_json_bytes(dictionary)
    sha = hashlib.sha256(raw).hexdigest()
    cold: list[float] = []
    parsed = None
    for _ in range(iterations):
        started = time.perf_counter_ns()
        fetched = memoryview(raw).tobytes()
        if hashlib.sha256(fetched).hexdigest() != sha:
            raise RuntimeError("mock checksum mismatch")
        parsed = build._validate_locator_dictionary(
            json.loads(fetched), dictionary["overture_release"]
        )
        cold.append((time.perf_counter_ns() - started) / 1000)
    assert parsed is not None
    cached: list[float] = []
    count = len(parsed["source_files"])
    for i in range(iterations * 10):
        started = time.perf_counter_ns()
        item = parsed["source_files"][i % count] if count else None
        if item:
            _ = item["theme"], item["feature_type"], item["filename"]
        cached.append((time.perf_counter_ns() - started) / 1000)
    return {
        "dictionary_bytes": len(raw),
        "dictionary_sha256": sha,
        "iterations": iterations,
        "cold_mock_fetch_sha_parse_validate_us": {
            "p50": round(statistics.median(cold), 3),
            "p95": round(percentile(cold, 0.95), 3),
            "max": round(max(cold), 3),
        },
        "cached_dictionary_lookup_us": {
            "p50": round(statistics.median(cached), 3),
            "p95": round(percentile(cached, 0.95), 3),
            "max": round(max(cached), 3),
        },
        "scope_warning": "Native Python local mock excludes network and Rust/wasm allocation overhead.",
    }


def inventory_recommendation(prefixes: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "design": "Registry range and release type jobs emit canonical content-addressed inventories. Stage markers bind inventory SHA/scope; the dictionary fan-in validates complete non-overlapping scopes and unions only inventories.",
        "requirements": [
            "retries replace inventory only with its marker and never reuse marker-less output",
            "missing, overlapping, duplicate-scope, stale-release, corrupt, or bad-SHA inventories fail closed",
            "build and patch jobs remain bound to the permanent manifest dictionary SHA",
            "patch rows with unseen tuples/releases still fail before COPY",
        ],
        "measured_real_prefix_rows": sum(item["rows"] for item in prefixes),
        "limitation": "A footer/stat audit cannot prove arbitrary DISTINCT value completeness; marker-bound inventories or a row scan are required.",
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Compact ID v3 scale gates",
        "",
        report["safety"],
        "",
        "## Dictionary discovery",
        "",
    ]
    discovery = report.get("dictionary_discovery")
    if discovery:
        lines.append(
            f"- Release tuples: **{discovery['release']['distinct_source_tuples']:,}** in **{discovery['release']['elapsed_seconds']:.3f}s**",
        )
        historical = discovery.get("historical") or {}
        if historical.get("status") == "blocked":
            lines.append(f"- Historical releases: **blocked** — {historical['error']}")
        else:
            lines.append(
                f"- Historical releases: **{historical['distinct_releases']:,}** in **{historical['elapsed_seconds']:.3f}s**"
            )
        dictionary = discovery.get("dictionary")
        if dictionary:
            lines.append(
                f"- Dictionary ({dictionary['status']}): **{dictionary['bytes']:,} bytes**"
            )
    else:
        lines.append("Remote discovery was not run; no result was fabricated.")
    lines += ["", "## Real registry-prefix transcodes", ""]
    for item in report["prefixes"]:
        lines.append(
            f"- `{item['prefix']}` ({item['size_class']}, {item['rows']:,} rows): "
            f"v1 **{item['v1_bytes']:,} B** → v3 **{item['bytes']:,} B** "
            f"(**+{item['v3_added_bytes_per_row']:.4f} B/row**, "
            f"{item['v3_storage_percent']:.3f}% of v1); footer "
            f"**{item['footer_bytes']:,} B**, footer reads "
            f"**{item['footer_range_reads']}**, cold range p50 "
            f"**{item['cold_lookup_range_bytes']['p50']:,} B**"
        )
    timing = report["dictionary_cold_cached"]
    lines += [
        "",
        "## First-cold dictionary proxy",
        "",
        f"- Dictionary: **{timing['dictionary_bytes']:,} bytes**",
        f"- Cold copy+SHA+parse+validate p50/p95: **{timing['cold_mock_fetch_sha_parse_validate_us']['p50']:.3f} / {timing['cold_mock_fetch_sha_parse_validate_us']['p95']:.3f} µs**",
        f"- Cached lookup p50/p95: **{timing['cached_dictionary_lookup_us']['p50']:.3f} / {timing['cached_dictionary_lookup_us']['p95']:.3f} µs**",
        f"- {timing['scope_warning']}",
        "",
        "## Recommendation",
        "",
        report["inventory_recommendation"]["design"],
        "",
        report["inventory_recommendation"]["limitation"],
        "",
        "The existing 700/1,000-source synthetic results are comparators, not substitutes.",
    ]
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", default=DEFAULT_RELEASE)
    parser.add_argument("--prefixes", default=",".join(DEFAULT_PREFIXES))
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--run-remote", action="store_true")
    parser.add_argument("--deadline-seconds", type=int, default=600)
    parser.add_argument("--overall-deadline-seconds", type=int, default=1800)
    parser.add_argument("--prefix-row-cap", type=int, default=2_000_000)
    parser.add_argument("--prefix-byte-cap", type=int, default=512 * 1024 * 1024)
    parser.add_argument("--memory-limit", default="4GB")
    parser.add_argument("--temp-byte-cap", type=int, default=8 * 1024 * 1024 * 1024)
    parser.add_argument("--historical-release-cap", type=int, default=256)
    parser.add_argument("--row-group-size", type=int, default=50_000)
    parser.add_argument("--dictionary-iterations", type=int, default=100)
    parser.add_argument("--json-out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    prefixes = tuple(x.strip().lower() for x in args.prefixes.split(",") if x.strip())
    if len(prefixes) != 3 or len(set(prefixes)) != 3:
        raise ValueError("exactly three distinct prefixes are required")
    args.work_dir.mkdir(parents=True, exist_ok=True)
    temp_dir = args.work_dir / "duckdb-temp"
    connection = duckdb.connect()
    configure_remote(connection, temp_dir, args.memory_limit, args.temp_byte_cap)
    extraction: list[dict[str, Any]] = []
    dictionary_discovery = None
    source_files = None
    historical = None
    release_metrics = None
    historical_metrics = None
    overall_started = time.monotonic()

    def remaining_deadline() -> int:
        remaining = args.overall_deadline_seconds - int(
            time.monotonic() - overall_started
        )
        if remaining <= 0:
            raise RuntimeError("overall remote deadline exhausted")
        return min(args.deadline_seconds, remaining)

    try:
        prefix_paths = [
            args.work_dir / f"registry-{prefix}.parquet" for prefix in prefixes
        ]
        if args.run_remote:
            source_files, release_metrics = discover_release_file_tuples(
                connection, args.release, remaining_deadline(), temp_dir
            )
            for prefix, path in zip(prefixes, prefix_paths, strict=True):
                extraction.append(
                    extract_registry_prefix(
                        connection,
                        prefix,
                        path,
                        args.prefix_row_cap,
                        args.prefix_byte_cap,
                        remaining_deadline(),
                        temp_dir,
                    )
                )
        else:
            missing = [str(path) for path in prefix_paths if not path.is_file()]
            if missing:
                raise RuntimeError("missing local prefix input: " + ", ".join(missing))
        prefix_dictionary = dictionary_from_prefixes(
            connection, prefix_paths, args.release
        )
        built = []
        for prefix, source in zip(prefixes, prefix_paths, strict=True):
            baseline_output = args.work_dir / f"baseline-v1-{prefix}.parquet"
            baseline = write_v1_prefix(
                connection, source, baseline_output, args.row_group_size
            )
            output = args.work_dir / f"compact-v3-{prefix}.parquet"
            metrics = write_compact_prefix(
                connection, source, output, prefix_dictionary, args.row_group_size
            )
            if baseline["v1_rows"] != metrics["rows"]:
                raise RuntimeError(f"prefix {prefix} v1/v3 row count mismatch")
            delta = metrics["bytes"] - baseline["v1_bytes"]
            built.append(
                {
                    "prefix": prefix,
                    **baseline,
                    **metrics,
                    "v3_added_bytes": delta,
                    "v3_added_bytes_per_row": round(delta / metrics["rows"], 4),
                    "v3_storage_percent": round(
                        metrics["bytes"] * 100 / baseline["v1_bytes"], 3
                    ),
                    **summarize_compact_prefix(connection, output),
                }
            )
        ordered = sorted(built, key=lambda x: (x["rows"], x["prefix"]))
        for index, item in enumerate(ordered):
            item["size_class"] = ("small", "median", "large")[index]
        lookup = {x["prefix"]: x for x in ordered}
        built = [lookup[x] for x in prefixes]

        # Preserve the cheap release inventory and real-prefix gates even if
        # the global historical DISTINCT reaches its deadline.
        if args.run_remote:
            try:
                historical, historical_metrics = discover_historical_releases(
                    connection,
                    remaining_deadline(),
                    args.historical_release_cap,
                    temp_dir,
                )
            except RuntimeError as error:
                historical_metrics = {
                    "status": "blocked",
                    "error": str(error),
                    "method": "exact global DISTINCT over path-null registry rows",
                }

        if source_files is not None and historical is not None:
            timing_dictionary = build._make_locator_dictionary(
                source_files, historical, args.release
            )
            raw = build._canonical_json_bytes(timing_dictionary)
            dictionary_summary = {
                "status": "complete",
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "source_files_count": timing_dictionary["source_files_count"],
                "last_seen_releases_count": timing_dictionary[
                    "last_seen_releases_count"
                ],
            }
        elif source_files is not None:
            timing_dictionary = build._make_locator_dictionary(
                source_files,
                prefix_dictionary["last_seen_releases"],
                args.release,
            )
            raw = build._canonical_json_bytes(timing_dictionary)
            dictionary_summary = {
                "status": "timing_proxy_only",
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "source_files_count": timing_dictionary["source_files_count"],
                "last_seen_releases_count": timing_dictionary[
                    "last_seen_releases_count"
                ],
                "warning": "Historical release universe is incomplete; do not build from this proxy.",
            }
        else:
            timing_dictionary = prefix_dictionary
            dictionary_summary = None

        if release_metrics is not None:
            dictionary_discovery = {
                "release": release_metrics,
                "historical": historical_metrics,
                "dictionary": dictionary_summary,
                "baseline": (
                    "Current implementation runs one global DISTINCT over registry and "
                    "release staging rows. This report separates exact pinned-release "
                    "file inventory from path-null historical discovery."
                ),
            }
        timing = benchmark_dictionary_cold_cached(
            timing_dictionary,
            args.dictionary_iterations,
        )
    finally:
        connection.close()
    report = {
        "report_version": REPORT_VERSION,
        "release": args.release,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "environment": {
            "python": platform.python_version(),
            "duckdb": duckdb.__version__,
            "platform": platform.platform(),
            "command": sys.argv,
            "memory_limit": args.memory_limit,
            "temp_byte_cap": args.temp_byte_cap,
            "overall_deadline_seconds": args.overall_deadline_seconds,
            "overall_elapsed_seconds": round(time.monotonic() - overall_started, 3),
        },
        "dictionary_discovery": dictionary_discovery,
        "prefix_extraction": extraction,
        "prefix_dictionary": {
            "source_files_count": prefix_dictionary["source_files_count"],
            "last_seen_releases_count": prefix_dictionary["last_seen_releases_count"],
            "bytes": len(build._canonical_json_bytes(prefix_dictionary)),
            "scope_warning": "Three real registry prefixes only; direct addresses/base release rows excluded.",
        },
        "prefixes": built,
        "dictionary_cold_cached": timing,
        "inventory_recommendation": inventory_recommendation(built),
        "synthetic_comparators": {
            "700_sources_added_bytes_per_row": 1.6995,
            "1000_sources_added_bytes_per_row": 1.7440,
            "scope": "previous synthetic acceptance; not a real-prefix substitute",
        },
        "safety": "Local temporary output only; no production shard, R2 object, catalog, Worker, deployment, or rebuild changed.",
    }
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    args.markdown_out.write_text(render_markdown(report))


if __name__ == "__main__":
    main()
