#!/usr/bin/env python3
"""Verify and query freshly downloaded Monaco shard-smoke R2 objects."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
from pathlib import Path
from typing import Any


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _assert_artifact(path: Path, record: dict[str, Any], label: str) -> None:
    expected_size = record.get("size_bytes")
    expected_sha = record.get("sha256")
    if not isinstance(expected_size, int) or expected_size <= 0:
        raise RuntimeError(f"{label} metadata has no valid size")
    if not isinstance(expected_sha, str) or len(expected_sha) != 64:
        raise RuntimeError(f"{label} metadata has no valid SHA-256")
    actual_size = path.stat().st_size
    actual_sha = _sha256(path)
    if actual_size != expected_size or actual_sha != expected_sha:
        raise RuntimeError(
            f"{label} readback mismatch: size={actual_size}/{expected_size} "
            f"sha256={actual_sha}/{expected_sha}"
        )


def _query_forward(path: Path) -> dict[str, Any]:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = db.execute(
            """
            SELECT d.gers_id, d.primary_name, d.country
            FROM divisions_fts
            JOIN divisions d ON d.rowid = divisions_fts.rowid
            WHERE divisions_fts MATCH 'monaco'
              AND d.country = 'MC'
            ORDER BY d.importance DESC, d.gers_id
            LIMIT 1
            """
        ).fetchone()
    finally:
        db.close()
    if row is None:
        raise RuntimeError("fresh forward shard returned no Monaco FTS result")
    return {"gers_id": row[0], "name": row[1], "country": row[2]}


def _query_reverse(path: Path) -> dict[str, Any]:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        seed = db.execute(
            """
            SELECT bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax
            FROM divisions_reverse
            WHERE country = 'MC'
            ORDER BY area, rowid
            LIMIT 1
            """
        ).fetchone()
        if seed is None:
            raise RuntimeError("fresh reverse shard has no Monaco row")
        lon = (seed[0] + seed[2]) / 2.0
        lat = (seed[1] + seed[3]) / 2.0
        row = db.execute(
            """
            SELECT d.gers_id, d.primary_name, d.country, d.subtype
            FROM divisions_reverse_rtree r
            JOIN divisions_reverse d ON d.rowid = r.id
            WHERE r.xmin <= ? AND r.xmax >= ?
              AND r.ymin <= ? AND r.ymax >= ?
              AND d.country = 'MC'
            ORDER BY d.area, d.rowid
            LIMIT 1
            """,
            (lon, lon, lat, lat),
        ).fetchone()
    finally:
        db.close()
    if row is None:
        raise RuntimeError("fresh reverse shard R-tree returned no Monaco result")
    return {
        "gers_id": row[0],
        "name": row[1],
        "country": row[2],
        "subtype": row[3],
    }


def _query_router(path: Path, record: dict[str, Any]) -> dict[str, Any]:
    expected_counts: dict[str, int] = {}
    for key in ("pair_count", "token_count"):
        value = record.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RuntimeError(f"router metadata has no valid {key}")
        expected_counts[key] = value

    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        integrity = db.execute("PRAGMA integrity_check").fetchall()
        if integrity != [("ok",)]:
            raise RuntimeError(f"fresh router failed integrity check: {integrity}")

        columns = db.execute("PRAGMA table_info(router)").fetchall()
        schema = [(row[1], row[2], row[3], row[5]) for row in columns]
        expected_schema = [
            ("token", "TEXT", 1, 1),
            ("shard_id", "TEXT", 1, 2),
            ("max_importance", "REAL", 1, 0),
        ]
        if schema != expected_schema:
            raise RuntimeError(f"fresh router has unexpected schema: {schema}")

        indexes = db.execute("PRAGMA index_list(router)").fetchall()
        index_columns: dict[str, list[str]] = {}
        for index in indexes:
            name = index[1]
            quoted_name = '"' + name.replace('"', '""') + '"'
            index_columns[name] = [
                row[2]
                for row in db.execute(f"PRAGMA index_info({quoted_name})").fetchall()
            ]
        has_token_index = any(
            index[1] == "idx_token"
            and index[2] == 0
            and index[3] == "c"
            and index[4] == 0
            and index_columns[index[1]] == ["token"]
            for index in indexes
        )
        has_primary_key_index = any(
            index[2] == 1
            and index[3] == "pk"
            and index_columns[index[1]] == ["token", "shard_id"]
            for index in indexes
        )
        if not has_token_index or not has_primary_key_index:
            raise RuntimeError("fresh router is missing its token or primary-key index")

        pair_count, token_count = db.execute(
            "SELECT count(*), count(DISTINCT token) FROM router"
        ).fetchone()
        actual_counts = {"pair_count": pair_count, "token_count": token_count}
        if actual_counts != expected_counts:
            raise RuntimeError(
                "fresh router metadata count mismatch: "
                f"database={actual_counts} metadata={expected_counts}"
            )

        token_row = db.execute(
            """
            SELECT token
            FROM router
            ORDER BY token
            LIMIT 1
            """
        ).fetchone()
        routes = []
        if token_row is not None:
            routes = db.execute(
                """
                SELECT shard_id, max_importance
                FROM router
                WHERE token = ?
                ORDER BY max_importance DESC
                LIMIT 4
                """,
                (token_row[0],),
            ).fetchall()
    finally:
        db.close()
    if token_row is None:
        if pair_count != 0 or token_count != 0:
            raise RuntimeError("fresh router count and sample query disagree")
        return {"row_count": 0, "sample": None}
    if pair_count <= 0 or token_count <= 0 or not routes:
        raise RuntimeError("fresh router count and sample query disagree")
    token = token_row[0]
    if not isinstance(token, str) or not token:
        raise RuntimeError(f"fresh router returned an invalid sample token: {token}")
    for route in routes:
        shard_id, importance = route
        if (
            not isinstance(shard_id, str)
            or not shard_id
            or not isinstance(importance, (int, float))
            or isinstance(importance, bool)
            or not math.isfinite(importance)
        ):
            raise RuntimeError(f"fresh router returned an invalid route: {route}")
    shard_id, importance = routes[0]
    return {
        "row_count": pair_count,
        "sample": {
            "token": token,
            "shard_id": shard_id,
            "importance": importance,
        },
    }


def verify_readback(
    collection_path: Path,
    reverse_collection_path: Path,
    forward_db: Path,
    reverse_db: Path,
    router_db: Path,
) -> dict[str, Any]:
    collection = json.loads(collection_path.read_text())
    reverse_collection = json.loads(reverse_collection_path.read_text())
    forward_record = collection.get("items", {}).get("MC")
    reverse_record = reverse_collection.get("items", {}).get("MC")
    router_record = collection.get("router")
    if not all(isinstance(record, dict) for record in (
        forward_record, reverse_record, router_record
    )):
        raise RuntimeError("smoke collections do not reference MC shards and router")
    if forward_record.get("href") != "./shards/MC.db":
        raise RuntimeError("smoke collection MC href is not ./shards/MC.db")
    if reverse_record.get("href") != "./reverse/MC.db":
        raise RuntimeError("smoke reverse collection MC href is not ./reverse/MC.db")
    if router_record.get("href") != "./router.db":
        raise RuntimeError("smoke collection router href is not ./router.db")
    _assert_artifact(forward_db, forward_record, "forward MC shard")
    _assert_artifact(reverse_db, reverse_record, "reverse MC shard")
    _assert_artifact(router_db, router_record, "router")
    return {
        "forward": _query_forward(forward_db),
        "reverse": _query_reverse(reverse_db),
        "router": _query_router(router_db, router_record),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--collection", type=Path, required=True)
    parser.add_argument("--reverse-collection", type=Path, required=True)
    parser.add_argument("--forward-db", type=Path, required=True)
    parser.add_argument("--reverse-db", type=Path, required=True)
    parser.add_argument("--router-db", type=Path, required=True)
    args = parser.parse_args()
    report = verify_readback(
        args.collection,
        args.reverse_collection,
        args.forward_db,
        args.reverse_db,
        args.router_db,
    )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
