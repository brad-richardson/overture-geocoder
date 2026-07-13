#!/usr/bin/env python3
"""Select deterministic v3 smoke cases and write an isolated preview catalog."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

import duckdb


FIXED_SMOKE_VERSION = "smoketest-id"
VERSION_RE = re.compile(r"^smoketest-id$")


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def build_preview_catalog(version: str) -> dict[str, Any]:
    if not VERSION_RE.fullmatch(version):
        raise ValueError("ID preview catalog must use the fixed smoketest-id version")
    return {
        "type": "Catalog",
        "stac_version": "1.1.0",
        "id": "geocoder-id-smoke",
        "description": "Isolated merge-only ID-index smoke catalog",
        "links": [
            {
                "rel": "self",
                "href": "./catalog.json",
                "type": "application/json",
            },
            {
                "rel": "child",
                "href": "./id-collection.json",
                "type": "application/json",
                "latest": True,
            },
        ],
    }


def select_preview_cases(
    con: duckdb.DuckDBPyConnection, sources: list[str]
) -> dict[str, Any]:
    if not sources:
        raise RuntimeError("ID smoke produced no parquet shards")
    source_sql = ", ".join(_sql_literal(source) for source in sorted(sources))
    relation = f"read_parquet([{source_sql}], union_by_name=true)"
    columns = (
        "CAST(id AS VARCHAR) AS id, bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax, "
        "source_file_id, last_seen_release_id, registry_member"
    )
    current = con.execute(
        f"SELECT {columns} FROM {relation} "
        "WHERE source_file_id IS NOT NULL "
        "AND last_seen_release_id IS NULL ORDER BY id LIMIT 1"
    ).fetchone()
    historical = con.execute(
        f"SELECT {columns} FROM {relation} "
        "WHERE source_file_id IS NULL "
        "AND last_seen_release_id IS NOT NULL "
        "AND registry_member = true ORDER BY id LIMIT 1"
    ).fetchone()
    if current is None:
        raise RuntimeError(
            "ID smoke has no current-release v3 row; release sampling is incomplete"
        )
    if historical is None:
        raise RuntimeError(
            "ID smoke has no retained historical v3 row in its registry prefixes"
        )

    names = [
        "id",
        "bbox_xmin",
        "bbox_ymin",
        "bbox_xmax",
        "bbox_ymax",
        "source_file_id",
        "last_seen_release_id",
        "registry_member",
    ]

    def portable(row: tuple[Any, ...]) -> dict[str, Any]:
        result = dict(zip(names, row))
        result["id"] = str(result["id"]).lower()
        return result

    return {"current": portable(current), "historical": portable(historical)}


def bind_expected_releases(
    cases: dict[str, Any], dictionary: dict[str, Any], current_release: str
) -> dict[str, Any]:
    releases = dictionary.get("last_seen_releases")
    if not isinstance(releases, list):
        raise RuntimeError("ID locator dictionary has no historical release list")
    release_id = cases["historical"].get("last_seen_release_id")
    if not isinstance(release_id, int) or not 1 <= release_id <= len(releases):
        raise RuntimeError("historical smoke case has an invalid compact release ID")
    historical_release = releases[release_id - 1]
    if not isinstance(historical_release, str) or not historical_release:
        raise RuntimeError("historical smoke case expands to an invalid release")
    if historical_release == current_release:
        raise RuntimeError("historical smoke case unexpectedly expands to current release")
    cases["current"]["expected_last_seen_release"] = current_release
    cases["historical"]["expected_last_seen_release"] = historical_release
    return cases


def bind_expected_current_locator(
    cases: dict[str, Any], dictionary: dict[str, Any], current_release: str
) -> dict[str, Any]:
    source_files = dictionary.get("source_files")
    if not isinstance(source_files, list):
        raise RuntimeError("ID locator dictionary has no source-file list")
    source_file_id = cases["current"].get("source_file_id")
    if (
        not isinstance(source_file_id, int)
        or not 1 <= source_file_id <= len(source_files)
    ):
        raise RuntimeError("current smoke case has an invalid compact source-file ID")
    entry = source_files[source_file_id - 1]
    if not isinstance(entry, dict):
        raise RuntimeError("current smoke case expands to an invalid source-file entry")
    theme = entry.get("theme")
    feature_type = entry.get("feature_type")
    filename = entry.get("filename")
    if (
        not isinstance(theme, str)
        or not theme
        or not isinstance(feature_type, str)
        or not feature_type
        or not isinstance(filename, str)
        or not filename.endswith(".parquet")
    ):
        raise RuntimeError("current smoke case expands to an invalid source-file entry")
    current = cases["current"]
    if not isinstance(current.get("registry_member"), bool):
        raise RuntimeError("current smoke case has an invalid registry status")
    current["expected_feature_type"] = feature_type
    current["expected_theme"] = theme
    current["expected_filename"] = filename
    current["expected_registry_member"] = current["registry_member"]
    current["expected_overture_path"] = (
        f"release/{current_release}/theme={theme}/type={feature_type}/{filename}"
    )
    current["expected_bbox"] = {
        "xmin": current["bbox_xmin"],
        "ymin": current["bbox_ymin"],
        "xmax": current["bbox_xmax"],
        "ymax": current["bbox_ymax"],
    }
    historical = cases["historical"]
    historical["expected_bbox"] = {
        "xmin": historical["bbox_xmin"],
        "ymin": historical["bbox_ymin"],
        "xmax": historical["bbox_xmax"],
        "ymax": historical["bbox_ymax"],
    }
    return cases


def _read_r2_json(
    con: duckdb.DuckDBPyConnection, path: str
) -> dict[str, Any]:
    row = con.execute("SELECT content FROM read_text(?)", [path]).fetchone()
    if row is None:
        raise RuntimeError(f"missing required ID smoke object {path}")
    payload = json.loads(row[0])
    if not isinstance(payload, dict):
        raise RuntimeError(f"invalid JSON object at {path}")
    return payload


def _r2_connection(bucket: str) -> duckdb.DuckDBPyConnection:
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID") or os.environ.get(
        "R2_ACCOUNT_ID"
    )
    key_id = os.environ.get("R2_ACCESS_KEY_ID")
    secret = os.environ.get("R2_SECRET_ACCESS_KEY")
    if not all((account_id, key_id, secret)):
        raise RuntimeError("R2 credentials are required for ID preview selection")
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("SET http_timeout=120000; SET http_retries=5;")
    con.execute(
        f"""
        CREATE SECRET r2 (
            TYPE S3,
            SCOPE {_sql_literal(f's3://{bucket}/')},
            KEY_ID {_sql_literal(key_id)},
            SECRET {_sql_literal(secret)},
            ENDPOINT {_sql_literal(f'{account_id}.r2.cloudflarestorage.com')},
            REGION 'auto',
            URL_STYLE 'path'
        )
        """
    )
    return con


def _glob_sources(con: duckdb.DuckDBPyConnection, pattern: str) -> list[str]:
    return sorted(
        row[0] for row in con.execute("SELECT file FROM glob(?)", [pattern]).fetchall()
    )


def prepare(
    version: str, release: str, bucket: str, output_dir: Path
) -> dict[str, Any]:
    catalog = build_preview_catalog(version)
    con = _r2_connection(bucket)
    try:
        pattern = f"s3://{bucket}/{version}/id-index/*.parquet"
        sources = _glob_sources(con, pattern)
        cases = select_preview_cases(con, sources)
        meta = _read_r2_json(con, f"s3://{bucket}/{version}/id-meta.json")
        reference = meta.get("locator_dictionary")
        if not isinstance(reference, dict):
            raise RuntimeError("ID smoke metadata has no v3 locator dictionary")
        href = reference.get("href")
        if not isinstance(href, str) or not href.startswith("./"):
            raise RuntimeError("ID smoke metadata has an invalid dictionary href")
        dictionary = _read_r2_json(
            con, f"s3://{bucket}/{version}/{href.removeprefix('./')}"
        )
        cases = bind_expected_releases(cases, dictionary, release)
        cases = bind_expected_current_locator(cases, dictionary, release)
    finally:
        con.close()

    output_dir.mkdir(parents=True, exist_ok=True)
    catalog_path = output_dir / "catalog.json"
    cases_path = output_dir / "id-smoke-cases.json"
    catalog_path.write_text(json.dumps(catalog, indent=2, sort_keys=True) + "\n")
    cases_path.write_text(json.dumps(cases, indent=2, sort_keys=True) + "\n")
    return {"catalog": catalog_path, "cases": cases_path, **cases}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", default=FIXED_SMOKE_VERSION)
    parser.add_argument("--release", required=True)
    parser.add_argument("--bucket", default="geocoder-shards")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    result = prepare(args.version, args.release, args.bucket, args.output_dir)
    print(
        "Prepared isolated ID smoke preview: "
        f"current={result['current']['id']} historical={result['historical']['id']}"
    )


if __name__ == "__main__":
    main()
