#!/usr/bin/env python3
"""Prove Monaco subset Parquet outputs equal legacy global outputs filtered to MC."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

import duckdb


SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from build_shards import (  # noqa: E402
    build_country_shard,
    build_global_router,
    build_reverse_country_shard,
    enrich_parquet_with_wiki_importance,
)


def _sql_path(path: Path) -> str:
    return "'" + str(path.resolve()).replace("'", "''") + "'"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot_sha256(snapshot: dict[str, Any]) -> str:
    encoded = json.dumps(
        snapshot, separators=(",", ":"), sort_keys=True
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _logical_rows_sha256(
    con: duckdb.DuckDBPyConnection, query: str
) -> str:
    rows = con.execute(f"SELECT * FROM ({query}) ORDER BY ALL").fetchall()
    portable = [
        [value.hex() if isinstance(value, bytes) else value for value in row]
        for row in rows
    ]
    encoded = json.dumps(
        portable, ensure_ascii=False, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def compare_parquet(
    con: duckdb.DuckDBPyConnection,
    legacy_path: Path,
    subset_path: Path,
    family: str,
) -> dict[str, Any]:
    legacy = f"SELECT * FROM read_parquet({_sql_path(legacy_path)}) WHERE country='MC'"
    subset = f"SELECT * FROM read_parquet({_sql_path(subset_path)})"
    legacy_schema = con.execute(f"DESCRIBE {legacy}").fetchall()
    subset_schema = con.execute(f"DESCRIBE {subset}").fetchall()
    if legacy_schema != subset_schema:
        raise RuntimeError(f"{family} schema differs from the legacy export")
    legacy_created_by = con.execute(
        "SELECT created_by FROM parquet_file_metadata(?)", [str(legacy_path)]
    ).fetchone()[0]
    subset_created_by = con.execute(
        "SELECT created_by FROM parquet_file_metadata(?)", [str(subset_path)]
    ).fetchone()[0]
    legacy_rows = con.execute(f"SELECT count(*) FROM ({legacy})").fetchone()[0]
    subset_rows = con.execute(f"SELECT count(*) FROM ({subset})").fetchone()[0]
    legacy_only = con.execute(
        f"SELECT count(*) FROM ({legacy} EXCEPT ALL {subset})"
    ).fetchone()[0]
    subset_only = con.execute(
        f"SELECT count(*) FROM ({subset} EXCEPT ALL {legacy})"
    ).fetchone()[0]
    if legacy_only or subset_only or legacy_rows != subset_rows:
        raise RuntimeError(
            f"{family} Monaco subset drifted: legacy_rows={legacy_rows} "
            f"subset_rows={subset_rows} legacy_only={legacy_only} subset_only={subset_only}"
        )
    return {
        "schema": [[row[0], row[1], row[2]] for row in legacy_schema],
        "rows": legacy_rows,
        "legacy_only_rows": legacy_only,
        "subset_only_rows": subset_only,
        "logical_rows_sha256": _logical_rows_sha256(con, subset),
        "legacy_created_by": legacy_created_by,
        "subset_created_by": subset_created_by,
        "legacy_sha256": _sha256(legacy_path),
        "subset_sha256": _sha256(subset_path),
    }


def _sqlite_snapshot(path: Path) -> dict[str, Any]:
    db = sqlite3.connect(path)
    try:
        schema = db.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
        tables = [
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        contents = {}
        for table in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            rows = db.execute(f"SELECT * FROM {quoted}").fetchall()
            if table == "metadata":
                rows = [row for row in rows if row[0] != "created_at"]
            contents[table] = sorted(
                [
                    [
                        value.hex() if isinstance(value, bytes) else value
                        for value in row
                    ]
                    for row in rows
                ],
                key=lambda row: json.dumps(row, sort_keys=True),
            )
        return {"schema": schema, "contents": contents}
    finally:
        db.close()


def _filter_country(source: Path, output: Path) -> None:
    con = duckdb.connect()
    try:
        con.execute(
            f"COPY (SELECT * FROM read_parquet({_sql_path(source)}) WHERE country='MC') "
            f"TO {_sql_path(output)} (FORMAT PARQUET, COMPRESSION ZSTD)"
        )
    finally:
        con.close()


def compare_built_shards(
    legacy_forward: Path,
    legacy_reverse: Path,
    subset_forward: Path,
    subset_reverse: Path,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="monaco-shard-equivalence-") as tmp:
        root = Path(tmp)
        legacy_forward_mc = root / "legacy-forward-mc.parquet"
        legacy_reverse_mc = root / "legacy-reverse-mc.parquet"
        _filter_country(legacy_forward, legacy_forward_mc)
        _filter_country(legacy_reverse, legacy_reverse_mc)
        built = {}
        for label, forward, reverse in (
            ("legacy", legacy_forward_mc, legacy_reverse_mc),
            ("subset", subset_forward, subset_reverse),
        ):
            enriched = root / f"{label}-forward-enriched.parquet"
            enrich_parquet_with_wiki_importance(forward, enriched, None)
            forward_db = root / f"{label}-forward.db"
            reverse_db = root / f"{label}-reverse.db"
            router_db = root / f"{label}-router.db"
            build_country_shard(enriched, "MC", forward_db, "equivalence")
            build_reverse_country_shard(reverse, "MC", reverse_db, "equivalence")
            build_global_router(enriched, router_db, version="equivalence")
            built[label] = {
                "forward": _sqlite_snapshot(forward_db),
                "reverse": _sqlite_snapshot(reverse_db),
                "router": _sqlite_snapshot(router_db),
            }

        comparisons = {}
        for family in ("forward", "reverse", "router"):
            equal = built["legacy"][family] == built["subset"][family]
            legacy = built["legacy"][family]
            subset = built["subset"][family]
            comparisons[family] = {
                "logical_contents_equal": equal,
                "legacy_snapshot_sha256": _snapshot_sha256(legacy),
                "subset_snapshot_sha256": _snapshot_sha256(subset),
                "schema": legacy["schema"],
                "legacy_table_row_counts": {
                    table: len(rows) for table, rows in legacy["contents"].items()
                },
                "subset_table_row_counts": {
                    table: len(rows) for table, rows in subset["contents"].items()
                },
            }
            if not equal:
                raise RuntimeError(
                    f"built {family} shard differs from legacy Monaco build"
                )
        return comparisons


def verify_exports(
    legacy_forward: Path,
    legacy_reverse: Path,
    subset_forward: Path,
    subset_reverse: Path,
    output: Path | None = None,
    compare_shards: bool = False,
    overture_release: str | None = None,
) -> dict[str, Any]:
    for path in (legacy_forward, legacy_reverse, subset_forward, subset_reverse):
        if not path.is_file():
            raise RuntimeError(f"missing comparison input {path}")
    con = duckdb.connect()
    try:
        report = {
            "equivalence_version": 1,
            "comparison": "schema-and-except-all-every-output-column",
            "overture_release": overture_release,
            "duckdb_python_version": duckdb.__version__,
            "forward": compare_parquet(con, legacy_forward, subset_forward, "forward"),
            "reverse": compare_parquet(con, legacy_reverse, subset_reverse, "reverse"),
        }
    finally:
        con.close()
    if compare_shards:
        report["built_shards"] = compare_built_shards(
            legacy_forward, legacy_reverse, subset_forward, subset_reverse
        )
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--legacy-forward", type=Path, required=True)
    parser.add_argument("--legacy-reverse", type=Path, required=True)
    parser.add_argument("--subset-forward", type=Path, required=True)
    parser.add_argument("--subset-reverse", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--compare-built-shards", action="store_true")
    parser.add_argument("--overture-release")
    args = parser.parse_args()
    report = verify_exports(
        args.legacy_forward,
        args.legacy_reverse,
        args.subset_forward,
        args.subset_reverse,
        args.output,
        args.compare_built_shards,
        args.overture_release,
    )
    print(
        "Monaco exports are equivalent: "
        f"{report['forward']['rows']} forward rows, "
        f"{report['reverse']['rows']} reverse rows"
    )


if __name__ == "__main__":
    main()
