#!/usr/bin/env python3
"""Measure Places GERS stability across two Overture releases.

The strongest available continuity key is not a place name or coordinate.  It
is the same upstream ``(dataset, record_id)`` appearing in both releases'
public bridge files.  This probe measures whether that stable source record is
assigned the same GERS UUID in both releases, while reporting ambiguous bridge
mappings separately rather than silently choosing one.

The output is decision evidence for a durable GERS -> external-entity sidecar.
It does not build or publish serving data and it never mutates an Overture
release.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import uuid


REPORT_SCHEMA = "overture-gers-place-stability-v1"
RELEASE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.\d+$")
DATASET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
MEMORY_LIMIT_RE = re.compile(r"^[1-9][0-9]{0,4}(?:KB|MB|GB|TB)$")
DEFAULT_DATASETS = (
    "AllThePlaces",
    "BrightQuery",
    "DAC",
    "Foursquare",
    "Krick",
    "Microsoft",
    "PinMeTo",
    "RenderSEO",
    "meta",
)


def canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode()


def bridge_glob(release: str, dataset: str) -> str:
    if not RELEASE_RE.fullmatch(release):
        raise ValueError(f"invalid Overture release: {release!r}")
    if not DATASET_RE.fullmatch(dataset):
        raise ValueError(f"invalid bridge dataset: {dataset!r}")
    return (
        "s3://overturemaps-us-west-2/bridgefiles/"
        f"{release}/dataset={dataset}/theme=places/type=place/*"
    )


def _valid_uuid(value: object) -> str | None:
    try:
        parsed = uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError):
        return None
    canonical = str(parsed)
    return canonical if str(value).lower() == canonical else None


def grouped_bridge_rows(rows: list[dict]) -> dict[tuple[str, str], set[str]]:
    """Return every valid source identity and all GERS IDs assigned to it."""

    grouped: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in rows:
        dataset = str(row.get("dataset") or "")
        record_id = str(row.get("record_id") or "")
        identifier = _valid_uuid(row.get("id"))
        if not DATASET_RE.fullmatch(dataset) or not record_id or identifier is None:
            continue
        grouped[(dataset, record_id)].add(identifier)
    return dict(grouped)


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 9) if denominator else None


def summarize_grouped(
    old: dict[tuple[str, str], set[str]],
    new: dict[tuple[str, str], set[str]],
    dataset: str | None = None,
) -> dict:
    if dataset is None:
        old_keys = set(old)
        new_keys = set(new)
    else:
        old_keys = {key for key in old if key[0] == dataset}
        new_keys = {key for key in new if key[0] == dataset}
    shared = old_keys & new_keys
    eligible = {
        key for key in shared if len(old[key]) == 1 and len(new[key]) == 1
    }
    stable = {
        key for key in eligible if next(iter(old[key])) == next(iter(new[key]))
    }
    reassigned = eligible - stable
    ambiguous = shared - eligible
    return {
        "old_source_keys": len(old_keys),
        "new_source_keys": len(new_keys),
        "shared_source_keys": len(shared),
        "old_only_source_keys": len(old_keys - new_keys),
        "new_only_source_keys": len(new_keys - old_keys),
        "ambiguous_shared_source_keys": len(ambiguous),
        "comparable_shared_source_keys": len(eligible),
        "stable_gers_ids": len(stable),
        "reassigned_gers_ids": len(reassigned),
        "shared_source_key_rate_from_old": _ratio(len(shared), len(old_keys)),
        "stable_gers_rate": _ratio(len(stable), len(eligible)),
    }


def summarize_bridge_rows(old_rows: list[dict], new_rows: list[dict]) -> dict:
    """Pure fixture-sized implementation of the SQL report contract."""

    old = grouped_bridge_rows(old_rows)
    new = grouped_bridge_rows(new_rows)
    datasets = sorted({key[0] for key in old} | {key[0] for key in new})
    reassigned = []
    for key in sorted(set(old) & set(new)):
        if len(old[key]) == len(new[key]) == 1 and old[key] != new[key]:
            reassigned.append(
                {
                    "dataset": key[0],
                    "record_id": key[1],
                    "old_gers_id": next(iter(old[key])),
                    "new_gers_id": next(iter(new[key])),
                }
            )
    return {
        "overall": summarize_grouped(old, new),
        "by_dataset": {
            dataset: summarize_grouped(old, new, dataset) for dataset in datasets
        },
        "reassigned_examples": reassigned,
    }


def _sql_strings(values: list[str]) -> str:
    # All values passed here have already matched DATASET_RE/RELEASE_RE.
    return "[" + ",".join("'" + value + "'" for value in values) + "]"


def build_probe_sql(old_release: str, new_release: str, datasets: list[str]) -> str:
    old_paths = [bridge_glob(old_release, dataset) for dataset in datasets]
    new_paths = [bridge_glob(new_release, dataset) for dataset in datasets]
    old_literal = _sql_strings(old_paths)
    new_literal = _sql_strings(new_paths)
    return f"""
CREATE OR REPLACE TEMP TABLE old_mapping AS
SELECT dataset, record_id, min(id) AS id, count(DISTINCT id) AS id_count
FROM read_parquet({old_literal}, hive_partitioning=true, union_by_name=true)
WHERE record_id IS NOT NULL AND record_id <> ''
  AND try_cast(id AS UUID) IS NOT NULL
GROUP BY dataset, record_id;

CREATE OR REPLACE TEMP TABLE new_mapping AS
SELECT dataset, record_id, min(id) AS id, count(DISTINCT id) AS id_count
FROM read_parquet({new_literal}, hive_partitioning=true, union_by_name=true)
WHERE record_id IS NOT NULL AND record_id <> ''
  AND try_cast(id AS UUID) IS NOT NULL
GROUP BY dataset, record_id;
""".strip()


SUMMARY_SQL = """
WITH joined AS (
  SELECT
    coalesce(o.dataset, n.dataset) AS dataset,
    o.id AS old_id,
    n.id AS new_id,
    o.id_count AS old_id_count,
    n.id_count AS new_id_count
  FROM old_mapping o
  FULL OUTER JOIN new_mapping n USING (dataset, record_id)
)
SELECT
  CASE WHEN grouping(dataset) = 1 THEN '__overall__' ELSE dataset END AS dataset,
  count(*) FILTER (WHERE old_id IS NOT NULL) AS old_source_keys,
  count(*) FILTER (WHERE new_id IS NOT NULL) AS new_source_keys,
  count(*) FILTER (WHERE old_id IS NOT NULL AND new_id IS NOT NULL)
    AS shared_source_keys,
  count(*) FILTER (WHERE old_id IS NOT NULL AND new_id IS NULL)
    AS old_only_source_keys,
  count(*) FILTER (WHERE old_id IS NULL AND new_id IS NOT NULL)
    AS new_only_source_keys,
  count(*) FILTER (
    WHERE old_id IS NOT NULL AND new_id IS NOT NULL
      AND (old_id_count <> 1 OR new_id_count <> 1)
  ) AS ambiguous_shared_source_keys,
  count(*) FILTER (
    WHERE old_id IS NOT NULL AND new_id IS NOT NULL
      AND old_id_count = 1 AND new_id_count = 1
  ) AS comparable_shared_source_keys,
  count(*) FILTER (
    WHERE old_id = new_id AND old_id_count = 1 AND new_id_count = 1
  ) AS stable_gers_ids,
  count(*) FILTER (
    WHERE old_id <> new_id AND old_id_count = 1 AND new_id_count = 1
  ) AS reassigned_gers_ids
FROM joined
GROUP BY GROUPING SETS ((dataset), ())
ORDER BY dataset;
""".strip()


EXAMPLES_SQL = """
SELECT o.dataset, o.record_id, o.id AS old_gers_id, n.id AS new_gers_id
FROM old_mapping o
JOIN new_mapping n USING (dataset, record_id)
WHERE o.id_count = 1 AND n.id_count = 1 AND o.id <> n.id
ORDER BY o.dataset, o.record_id
LIMIT ?;
""".strip()


def _complete_rates(row: dict) -> dict:
    result = {key: int(value) if isinstance(value, int) else value
              for key, value in row.items() if key != "dataset"}
    result["shared_source_key_rate_from_old"] = _ratio(
        result["shared_source_keys"], result["old_source_keys"]
    )
    result["stable_gers_rate"] = _ratio(
        result["stable_gers_ids"], result["comparable_shared_source_keys"]
    )
    return result


def run_probe(args: argparse.Namespace) -> dict:
    try:
        import duckdb
    except ImportError as exception:  # pragma: no cover - environment failure
        raise RuntimeError("duckdb is required to run the public-data probe") from exception

    datasets = list(dict.fromkeys(args.dataset or DEFAULT_DATASETS))
    for dataset in datasets:
        if not DATASET_RE.fullmatch(dataset):
            raise ValueError(f"invalid bridge dataset: {dataset!r}")
    if not MEMORY_LIMIT_RE.fullmatch(args.memory_limit):
        raise ValueError(
            "memory limit must be an integer followed by KB, MB, GB, or TB"
        )
    sql = build_probe_sql(args.old_release, args.new_release, datasets)
    work_dir = args.work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(work_dir / "gers-stability.duckdb"))
    connection.execute(f"SET threads = {args.threads}")
    connection.execute(f"SET memory_limit = '{args.memory_limit}'")
    temp_directory = str(work_dir / "duckdb-tmp").replace("'", "''")
    connection.execute(f"SET temp_directory = '{temp_directory}'")
    connection.execute("SET enable_progress_bar = false")
    connection.execute(sql)
    columns = [item[0] for item in connection.execute(SUMMARY_SQL).description]
    summary_rows = [dict(zip(columns, row)) for row in connection.fetchall()]
    examples_columns = [
        item[0]
        for item in connection.execute(EXAMPLES_SQL, [args.example_limit]).description
    ]
    examples = [dict(zip(examples_columns, row)) for row in connection.fetchall()]
    version = connection.execute("SELECT version()").fetchone()[0]
    connection.close()

    raw_by_dataset = {
        row.pop("dataset"): _complete_rates(row)
        for row in summary_rows
    }
    overall = raw_by_dataset.pop("__overall__")
    report = {
        "schema": REPORT_SCHEMA,
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "old_release": args.old_release,
            "new_release": args.new_release,
            "theme": "places",
            "type": "place",
            "datasets": datasets,
            "identity_unit": ["dataset", "record_id"],
            "duckdb_version": version,
            "probe_sql_sha256": hashlib.sha256(sql.encode()).hexdigest(),
            "sources": {
                "old": [bridge_glob(args.old_release, value) for value in datasets],
                "new": [bridge_glob(args.new_release, value) for value in datasets],
            },
        },
        "measurement": {
            "overall": overall,
            "by_dataset": dict(sorted(raw_by_dataset.items())),
            "reassigned_examples": examples,
        },
        "interpretation": {
            "stable_gers_rate_denominator": (
                "shared (dataset, record_id) keys with exactly one GERS ID in each release"
            ),
            "scope": (
                "Measures identifier reuse for persistent upstream source records; "
                "it does not classify legitimate source additions/removals."
            ),
            "sidecar_consequence": (
                "Use a durable GERS-to-external-entity mapping plus an attested "
                "per-release membership/delta, not a full rematch by default."
            ),
        },
    }
    return report


def write_report(path: Path, report: dict) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f".{path.name}.tmp")
    staged.write_bytes(canonical_json(report))
    staged.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--old-release", default="2026-06-17.0")
    parser.add_argument("--new-release", default="2026-07-22.0")
    parser.add_argument("--dataset", action="append", default=[])
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--memory-limit", default="8GB")
    parser.add_argument("--example-limit", type=int, default=100)
    args = parser.parse_args(argv)
    if args.threads < 1 or args.threads > 64:
        parser.error("--threads must be between 1 and 64")
    if args.example_limit < 0 or args.example_limit > 1000:
        parser.error("--example-limit must be between 0 and 1000")
    try:
        report = run_probe(args)
        write_report(args.output, report)
    except (OSError, RuntimeError, ValueError) as exception:
        print(f"gers stability probe failed: {exception}", file=sys.stderr)
        return 2
    overall = report["measurement"]["overall"]
    stable_rate = overall["stable_gers_rate"]
    shown_rate = "n/a" if stable_rate is None else f"{stable_rate:.6%}"
    print(
        f"stable GERS IDs: {overall['stable_gers_ids']:,}/"
        f"{overall['comparable_shared_source_keys']:,} "
        f"({shown_rate}); reassigned: {overall['reassigned_gers_ids']:,}"
    )
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
