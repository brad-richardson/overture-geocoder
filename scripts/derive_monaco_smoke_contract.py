#!/usr/bin/env python3
"""Derive the Monaco smoke identity contract from one Overture release."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import duckdb


RELEASE_VERSION = re.compile(r"^\d{4}-\d{2}-\d{2}\.\d+$")


def build_contract(
    division_rows: list[dict[str, Any]],
    area_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the identities the production transforms must preserve."""
    required_divisions = [
        {"id": row["id"], "subtype": row["subtype"]}
        for row in division_rows
    ]
    required_areas = [
        {
            "id": row["id"],
            "division_id": row["division_id"],
            "subtype": row["subtype"],
        }
        for row in area_rows
    ]
    for label, rows in (
        ("forward-eligible divisions", required_divisions),
        ("division areas", required_areas),
    ):
        ids = [row["id"] for row in rows]
        if not ids:
            raise RuntimeError(f"latest Overture release has no Monaco {label}")
        if len(ids) != len(set(ids)):
            raise RuntimeError(f"latest Overture release has duplicate Monaco {label}")
    return {
        "contract_version": 1,
        "country_code": "MC",
        "required_divisions": sorted(required_divisions, key=lambda row: row["id"]),
        "required_areas": sorted(required_areas, key=lambda row: row["id"]),
    }


def _rows(connection: duckdb.DuckDBPyConnection, sql: str) -> list[dict[str, Any]]:
    cursor = connection.execute(sql)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def derive_contract(release: str) -> dict[str, Any]:
    if not RELEASE_VERSION.fullmatch(release):
        raise RuntimeError(f"invalid Overture release: {release!r}")
    source = (
        "s3://overturemaps-us-west-2/release/"
        f"{release}/theme=divisions/type={{feature_type}}/*"
    )
    connection = duckdb.connect()
    try:
        connection.execute("INSTALL httpfs; LOAD httpfs;")
        connection.execute("SET s3_region='us-west-2'; SET threads=4;")
        divisions = _rows(
            connection,
            f"""
            SELECT id, subtype
            FROM read_parquet(
                '{source.format(feature_type="division")}',
                hive_partitioning=true
            )
            WHERE country = 'MC'
              AND names.primary IS NOT NULL
              AND (
                  subtype IN ('country', 'region', 'county', 'localadmin')
                  OR (
                      subtype = 'locality'
                      AND (population > 10000 OR wikidata IS NOT NULL)
                  )
              )
            ORDER BY id
            """,
        )
        areas = _rows(
            connection,
            f"""
            SELECT id, division_id, subtype
            FROM read_parquet(
                '{source.format(feature_type="division_area")}',
                hive_partitioning=true
            )
            WHERE country = 'MC'
            ORDER BY id
            """,
        )
    finally:
        connection.close()
    return build_contract(divisions, areas)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    contract = derive_contract(args.release)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(contract, indent=2, sort_keys=True) + "\n")
    print(
        f"Derived Monaco smoke contract for {args.release}: "
        f"{len(contract['required_divisions'])} forward divisions, "
        f"{len(contract['required_areas'])} division areas"
    )


if __name__ == "__main__":
    main()
