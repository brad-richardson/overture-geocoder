#!/usr/bin/env python3
"""Generate or run a bounded PostgreSQL Places search experiment.

Without ``--database-url`` this script is a deterministic model: it reads the
local fixture, writes PostgreSQL-compatible schema/query SQL, and reports only
logical payload and candidate counts.  With a real PostgreSQL ``psql`` client
and an explicit database URL it creates an isolated spike schema, loads the
fixture, captures relation sizes, and runs EXPLAIN (ANALYZE, BUFFERS, JSON).

The database mode is intentionally destructive only inside the dedicated
``places_planetscale_spike`` schema. It has no production integration.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Iterator


SCHEMA = "places_planetscale_spike"
TOKEN_RE = re.compile(r"[\w]+", re.UNICODE)
RELEASE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


def normalize(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").strip().lower())
    return " ".join(
        TOKEN_RE.findall("".join(c for c in text if not unicodedata.combining(c)))
    )


def iter_rows(path: Path) -> Iterator[dict[str, Any]]:
    if path.suffix.lower() == ".parquet":
        try:
            import duckdb  # type: ignore
        except ImportError as exc:
            raise RuntimeError("Parquet input requires duckdb") from exc
        connection = duckdb.connect()
        try:
            cursor = connection.execute("SELECT * FROM read_parquet(?)", [str(path)])
            columns = [column[0] for column in cursor.description]
            while rows := cursor.fetchmany(10_000):
                for row in rows:
                    yield dict(zip(columns, row))
        finally:
            connection.close()
        return
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8-sig") as src:
            yield from csv.DictReader(src)
        return
    if path.suffix.lower() in {".jsonl", ".ndjson"}:
        with path.open(encoding="utf-8") as src:
            for line in src:
                if line.strip():
                    yield json.loads(line)
        return
    raise ValueError("input must be Parquet, CSV, or JSONL")


def clean_row(row: dict[str, Any], number: int) -> dict[str, Any] | None:
    name = str(row.get("primary_name") or row.get("name") or "").strip()
    if not name:
        return None
    raw_id = str(row.get("gers_id") or row.get("id") or "")
    try:
        place_id = str(uuid.UUID(raw_id))
    except ValueError:
        place_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"fixture:{raw_id or number}"))

    def number_value(key: str, default: float) -> float:
        try:
            return float(row.get(key))
        except (TypeError, ValueError):
            return default

    return {
        "gers_id": place_id,
        "name": name,
        "brand": str(row.get("brand_name") or "").strip(),
        "category": str(
            row.get("category_primary")
            or row.get("category")
            or row.get("basic_category")
            or ""
        ).strip(),
        "locality": str(row.get("locality") or row.get("city") or "").strip(),
        "region": str(row.get("region") or "").strip(),
        "country": str(row.get("country") or "").strip(),
        "lat": number_value("lat", 0.0),
        "lon": number_value("lon", 0.0),
        "confidence": min(1.0, max(0.0, number_value("confidence", 0.5))),
    }


def load_rows(path: Path) -> list[dict[str, Any]]:
    result = []
    for number, row in enumerate(iter_rows(path), 1):
        cleaned = clean_row(row, number)
        if cleaned:
            result.append(cleaned)
    if not result:
        raise ValueError("input contains no named Places")
    return result


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def partition_name(release_id: str) -> str:
    return "places_r_" + hashlib.sha256(release_id.encode()).hexdigest()[:12]


def schema_sql(
    release_id: str,
    overture_release: str,
    expected_rows: int = 0,
    source_sha256: str = "0" * 64,
) -> str:
    if not RELEASE_RE.fullmatch(release_id):
        raise ValueError("invalid release ID")
    partition = partition_name(release_id)
    release = sql_literal(release_id)
    overture = sql_literal(overture_release)
    if expected_rows < 0 or not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
        raise ValueError("invalid release inventory")
    return f"""-- Offline PlanetScale/PostgreSQL Places spike. Not production DDL.
DROP SCHEMA IF EXISTS {SCHEMA} CASCADE;
CREATE SCHEMA {SCHEMA};

CREATE TABLE {SCHEMA}.releases (
  release_id text PRIMARY KEY,
  overture_release text NOT NULL,
  state text NOT NULL CHECK (state IN ('loading', 'ready')),
  expected_rows bigint NOT NULL CHECK (expected_rows >= 0),
  loaded_rows bigint,
  source_sha256 text NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{{64}}$'),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE {SCHEMA}.catalog (
  family text PRIMARY KEY CHECK (family = 'places'),
  active_release_id text NOT NULL REFERENCES {SCHEMA}.releases(release_id)
);

CREATE TABLE {SCHEMA}.places (
  release_id text NOT NULL,
  gers_id uuid NOT NULL,
  name text NOT NULL,
  brand text NOT NULL DEFAULT '',
  category text NOT NULL DEFAULT '',
  locality text NOT NULL DEFAULT '',
  region text NOT NULL DEFAULT '',
  country text NOT NULL DEFAULT '',
  lat real NOT NULL CHECK (lat BETWEEN -90 AND 90),
  lon real NOT NULL CHECK (lon BETWEEN -180 AND 180),
  confidence real NOT NULL CHECK (confidence BETWEEN 0 AND 1),
  normalized_name text GENERATED ALWAYS AS (lower(name)) STORED,
  search_document tsvector GENERATED ALWAYS AS (
    setweight(to_tsvector('simple'::regconfig, coalesce(name, '')), 'A') ||
    setweight(to_tsvector('simple'::regconfig, coalesce(brand, '')), 'A') ||
    setweight(to_tsvector('simple'::regconfig, coalesce(category, '')), 'B') ||
    setweight(to_tsvector('simple'::regconfig,
      coalesce(locality, '') || ' ' || coalesce(region, '') || ' ' || coalesce(country, '')), 'C')
  ) STORED,
  PRIMARY KEY (release_id, gers_id)
) PARTITION BY LIST (release_id);

CREATE TABLE {SCHEMA}.{partition}
  PARTITION OF {SCHEMA}.places FOR VALUES IN ({release});
CREATE INDEX {partition}_fts_gin
  ON {SCHEMA}.{partition} USING gin (search_document);
CREATE INDEX {partition}_name_prefix
  ON {SCHEMA}.{partition} (normalized_name text_pattern_ops);
CREATE INDEX {partition}_category
  ON {SCHEMA}.{partition} (category);
CREATE INDEX {partition}_context
  ON {SCHEMA}.{partition} (country, region, locality);

INSERT INTO {SCHEMA}.releases
  (release_id, overture_release, state, expected_rows, source_sha256)
VALUES ({release}, {overture}, 'loading', {expected_rows},
  {sql_literal(source_sha256)});
"""


def representative_queries(release_id: str) -> dict[str, str]:
    release = sql_literal(release_id)
    base = f"FROM {SCHEMA}.places WHERE release_id = {release}"
    columns = "gers_id, name, category, locality, region, country, lat, lon, confidence"
    return {
        "name_prefix": f"SELECT {columns} {base} AND normalized_name LIKE 'starb%' ORDER BY confidence DESC, gers_id LIMIT 10",
        "token_exact": f"SELECT {columns} {base} AND search_document @@ plainto_tsquery('simple', 'warfield hotel') ORDER BY ts_rank_cd(search_document, plainto_tsquery('simple', 'warfield hotel')) DESC, confidence DESC, gers_id LIMIT 10",
        "token_prefix": f"SELECT {columns} {base} AND search_document @@ to_tsquery('simple', 'golden & gat:*') ORDER BY ts_rank_cd(search_document, to_tsquery('simple', 'golden & gat:*')) DESC, confidence DESC, gers_id LIMIT 10",
        "category": f"SELECT {columns} {base} AND category = 'hotel' ORDER BY confidence DESC, gers_id LIMIT 10",
        "context_token": f"SELECT {columns} {base} AND country = 'US' AND region = 'CA' AND locality = 'San Francisco' AND search_document @@ to_tsquery('simple', 'cafe:*') ORDER BY ts_rank_cd(search_document, to_tsquery('simple', 'cafe:*')) DESC, confidence DESC, gers_id LIMIT 10",
    }


def queries_sql(release_id: str) -> str:
    lines = [
        "-- Resolve active_release_id once, cache it briefly in the serving tier,",
        "-- then bind the concrete release ID below to preserve partition pruning.",
        f"SELECT active_release_id FROM {SCHEMA}.catalog WHERE family = 'places';",
        "",
    ]
    for name, query in representative_queries(release_id).items():
        lines += [f"-- {name}", query + ";", ""]
    return "\n".join(lines)


def fixture_model(rows: list[dict[str, Any]]) -> dict[str, Any]:
    text_fields = ("name", "brand", "category", "locality", "region", "country")
    logical = sum(
        16 + 12 + sum(len(row[field].encode()) for field in text_fields) for row in rows
    )
    token_occurrences = 0
    unique_field_tokens = set()
    searchable_fields = ("name", "brand", "category", "locality", "region", "country")
    for row in rows:
        for field in text_fields:
            field_tokens = normalize(row[field]).split()
            token_occurrences += len(field_tokens)
            unique_field_tokens.update((field, token) for token in field_tokens)
    counts = {
        "name_prefix_starb": sum(
            normalize(row["name"]).startswith("starb") for row in rows
        ),
        "token_exact_warfield_hotel": sum(
            {"warfield", "hotel"}.issubset(
                normalize(" ".join(row[field] for field in searchable_fields)).split()
            )
            for row in rows
        ),
        "token_prefix_golden_gate": sum(
            "golden"
            in normalize(" ".join(row[field] for field in searchable_fields)).split()
            and any(
                token.startswith("gat")
                for token in normalize(
                    " ".join(row[field] for field in searchable_fields)
                ).split()
            )
            for row in rows
        ),
        "category_hotel": sum(normalize(row["category"]) == "hotel" for row in rows),
        "sf_context_cafe_prefix": sum(
            row["country"] == "US"
            and row["region"] == "CA"
            and row["locality"] == "San Francisco"
            and any(
                token.startswith("cafe")
                for token in normalize(
                    " ".join((row["name"], row["brand"], row["category"]))
                ).split()
            )
            for row in rows
        ),
    }
    return {
        "row_count": len(rows),
        "logical_field_payload_bytes": logical,
        "logical_field_payload_bytes_per_place": logical / len(rows),
        "token_occurrences": token_occurrences,
        "unique_field_tokens": len(unique_field_tokens),
        "model_candidate_counts": counts,
        "linear_shape_only": {
            "one_million_logical_payload_bytes": round(logical / len(rows) * 1_000_000),
            "seventy_five_million_logical_payload_bytes": round(
                logical / len(rows) * 75_000_000
            ),
            "warning": "not PostgreSQL storage: excludes tuple/TOAST/page overhead, generated tsvector, GIN/B-tree indexes, WAL, replicas, and release retention",
        },
    }


def usable_psql() -> tuple[bool, str]:
    executable = shutil.which("psql")
    if not executable:
        return False, "psql not found"
    result = subprocess.run([executable, "--version"], text=True, capture_output=True)
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0 or not output.startswith("psql (PostgreSQL)"):
        return False, "PATH psql is not PostgreSQL psql"
    return True, output


def write_csv(path: Path, release_id: str, rows: list[dict[str, Any]]) -> None:
    fields = (
        "release_id",
        "gers_id",
        "name",
        "brand",
        "category",
        "locality",
        "region",
        "country",
        "lat",
        "lon",
        "confidence",
    )
    with path.open("w", newline="", encoding="utf-8") as dst:
        writer = csv.DictWriter(dst, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({"release_id": release_id, **row})


def run_psql(database_url: str, sql: str) -> str:
    env = dict(os.environ)
    env["PGCONNECT_TIMEOUT"] = env.get("PGCONNECT_TIMEOUT", "10")
    result = subprocess.run(
        [
            shutil.which("psql") or "psql",
            "-X",
            "-v",
            "ON_ERROR_STOP=1",
            "-At",
            database_url,
        ],
        input=sql,
        text=True,
        capture_output=True,
        env=env,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "psql failed")
    return result.stdout.strip()


def execute_database(
    database_url: str,
    schema: str,
    release_id: str,
    overture_release: str,
    source_sha: str,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    partition = partition_name(release_id)
    started = time.perf_counter()
    run_psql(database_url, schema)
    with tempfile.TemporaryDirectory() as directory:
        csv_path = Path(directory) / "places.csv"
        write_csv(csv_path, release_id, rows)
        copy = f"\\copy {SCHEMA}.places(release_id,gers_id,name,brand,category,locality,region,country,lat,lon,confidence) FROM {sql_literal(str(csv_path))} WITH (FORMAT csv, HEADER true)\n"
        run_psql(database_url, copy)
    finalize = f"""
BEGIN;
DO $$ BEGIN
  IF (SELECT count(*) FROM {SCHEMA}.{partition}) <> {len(rows)} THEN
    RAISE EXCEPTION 'row count mismatch';
  END IF;
END $$;
UPDATE {SCHEMA}.releases SET state='ready', loaded_rows={len(rows)} WHERE release_id={sql_literal(release_id)};
INSERT INTO {SCHEMA}.catalog(family,active_release_id) VALUES('places',{sql_literal(release_id)})
ON CONFLICT(family) DO UPDATE SET active_release_id=excluded.active_release_id;
COMMIT;
ANALYZE {SCHEMA}.{partition};
"""
    run_psql(database_url, finalize)
    size_sql = f"SELECT json_build_object('table_bytes',pg_table_size('{SCHEMA}.{partition}'),'indexes_bytes',pg_indexes_size('{SCHEMA}.{partition}'),'total_bytes',pg_total_relation_size('{SCHEMA}.{partition}'))::text;"
    sizes = json.loads(run_psql(database_url, size_sql))
    plans = {}
    for name, query in representative_queries(release_id).items():
        output = run_psql(
            database_url, "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + query + ";"
        )
        plans[name] = json.loads(output)
    return {
        "measured": True,
        "load_and_index_seconds": time.perf_counter() - started,
        "sizes": sizes,
        "plans": plans,
    }


def markdown(report: dict[str, Any]) -> str:
    model = report["fixture_model"]
    execution = report["database_execution"]
    lines = [
        "# PlanetScale Postgres Places spike",
        "",
        f"**Status: {'database-measured' if execution['measured'] else 'model-only; PostgreSQL was not executed'}**",
        "",
        f"- Fixture: `{report['input']}` ({model['row_count']:,} named Places)",
        f"- Release partition: `{report['release_id']}`",
        f"- Logical searchable/result payload: {model['logical_field_payload_bytes']:,} bytes ({model['logical_field_payload_bytes_per_place']:.1f} B/place)",
        f"- Token occurrences / unique field-token pairs: {model['token_occurrences']:,} / {model['unique_field_tokens']:,}",
        f"- Database execution: {execution.get('reason', 'completed')}",
        "",
        "PlanetScale documents standard PostgreSQL full-text search support and currently lists `pg_trgm` as supported. This spike uses only core `tsvector`/GIN plus B-tree prefix/context indexes; typo correction remains out of scope.",
        "",
        "## Release strategy",
        "",
        "Each immutable data release gets a LIST partition keyed by `release_id`. A release is loaded as `loading`, row-count validated, marked `ready`, and exposed by atomically changing one catalog row. The serving tier resolves that pointer, then binds the concrete release ID in search SQL so PostgreSQL can prune other retained release partitions. Rollback is another catalog-pointer transaction.",
        "",
        "PlanetScale Postgres branches are isolated deployments; a newly created development branch does not currently copy schema/data automatically, so this schema must be explicitly applied there. The benchmark never assumes branch merge/deploy-request behavior.",
        "",
        "For a hosted run, use a direct connection for schema creation and bulk load, then benchmark the application path through the pooled connection separately; PlanetScale documents those as distinct connection modes.",
        "",
        "## Fixture model",
        "",
        "| query shape | model candidates |",
        "|---|---:|",
    ]
    for name, count in model["model_candidate_counts"].items():
        lines.append(f"| {name} | {count:,} |")
    linear = model["linear_shape_only"]
    lines += [
        "",
        f"Linear logical payload only: {linear['one_million_logical_payload_bytes']:,} bytes at 1M rows and {linear['seventy_five_million_logical_payload_bytes']:,} bytes at 75M rows.",
        f"Warning: {linear['warning']}",
        "",
        "## Database evidence",
        "",
    ]
    if execution["measured"]:
        sizes = execution["sizes"]
        lines += [
            f"- Load/index time: {execution['load_and_index_seconds']:.3f}s",
            f"- Heap / indexes / total: {sizes['table_bytes']:,} / {sizes['indexes_bytes']:,} / {sizes['total_bytes']:,} bytes",
            "- Full JSON EXPLAIN ANALYZE plans are retained in the JSON report.",
        ]
    else:
        lines += [
            "No PostgreSQL server was available. There are no measured plans, relation/index sizes, network latency, concurrency results, or PlanetScale cost claims in this report.",
            "Run the command below with a disposable database URL to collect them; it drops only the dedicated `places_planetscale_spike` schema.",
            "",
            "```sh",
            "python3 scripts/experiment_places_postgres.py exports/experiment/places-raw.parquet \\",
            '  --database-url "$DATABASE_URL" --schema-out /tmp/places.sql --queries-out /tmp/queries.sql \\',
            "  --json-out /tmp/places-postgres.json --markdown-out /tmp/places-postgres.md",
            "```",
        ]
        lines += ["", "Expected plans below are hypotheses, not EXPLAIN output:"]
        for name, expectation in report["expected_unmeasured_plans"].items():
            lines.append(f"- `{name}`: {expectation}")
    lines += [
        "",
        "## Interpretation",
        "",
        "The plausible role is a remotely queried regional Places service, not a whole-loaded edge shard. PostgreSQL removes custom range-planning and publication-object fanout, but introduces a network/database dependency, retained-release index duplication, connection-pool behavior, and operational cost. A decision requires a real PlanetScale development-branch run with representative regional scale, concurrency, cold/warm latency, query plans, index bytes, and labelled ranking—not this 0.98 km² fixture model.",
        "",
        "Official references: [Postgres compatibility](https://planetscale.com/docs/postgres/postgres-compatibility), [extensions](https://planetscale.com/docs/postgres/extensions), [branching](https://planetscale.com/docs/postgres/branching), and [connections](https://planetscale.com/docs/postgres/connecting).",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--release-id", default="fixture-2025-12-17.0")
    parser.add_argument("--overture-release", default="2025-12-17.0")
    parser.add_argument("--database-url")
    parser.add_argument("--schema-out", type=Path, required=True)
    parser.add_argument("--queries-out", type=Path, required=True)
    parser.add_argument("--json-out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path, required=True)
    args = parser.parse_args(argv)

    if not RELEASE_RE.fullmatch(args.release_id):
        parser.error("invalid --release-id")
    rows = load_rows(args.input)
    source_sha = hashlib.sha256(args.input.read_bytes()).hexdigest()
    schema = schema_sql(args.release_id, args.overture_release, len(rows), source_sha)
    query_text = queries_sql(args.release_id)
    args.schema_out.parent.mkdir(parents=True, exist_ok=True)
    args.queries_out.parent.mkdir(parents=True, exist_ok=True)
    args.schema_out.write_text(schema)
    args.queries_out.write_text(query_text)

    available, psql_detail = usable_psql()
    if args.database_url and not available:
        parser.error("--database-url requires PostgreSQL psql: " + psql_detail)
    execution = (
        execute_database(
            args.database_url,
            schema,
            args.release_id,
            args.overture_release,
            source_sha,
            rows,
        )
        if args.database_url
        else {"measured": False, "reason": "no --database-url supplied; " + psql_detail}
    )
    report = {
        "schema_version": 1,
        "input": str(args.input),
        "source_sha256": source_sha,
        "release_id": args.release_id,
        "overture_release": args.overture_release,
        "postgres_schema": SCHEMA,
        "postgres_execution_available": available,
        "psql_detail": psql_detail,
        "fixture_model": fixture_model(rows),
        "representative_queries": representative_queries(args.release_id),
        "expected_unmeasured_plans": {
            "name_prefix": "B-tree range/index scan on normalized_name text_pattern_ops",
            "token_exact": "GIN bitmap index scan on search_document followed by top-k ranking",
            "token_prefix": "GIN bitmap index scan on search_document followed by top-k ranking",
            "category": "B-tree index scan on category followed by confidence sort",
            "context_token": "planner-dependent bitmap combination or one selective index plus filter across context B-tree and FTS GIN",
        },
        "database_execution": execution,
        "unmeasured_claims": []
        if execution["measured"]
        else [
            "PostgreSQL heap, generated tsvector, GIN and B-tree index bytes",
            "query plans and latency",
            "network and connection-pool overhead",
            "PlanetScale performance, concurrency, replicas, and cost",
        ],
    }
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(report, indent=2) + "\n")
    args.markdown_out.write_text(markdown(report) + "\n")
    print(
        json.dumps(
            {
                "rows": len(rows),
                "measured": execution["measured"],
                "reason": execution.get("reason"),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
