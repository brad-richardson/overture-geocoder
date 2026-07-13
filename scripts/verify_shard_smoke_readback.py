#!/usr/bin/env python3
"""Verify and query freshly downloaded Monaco shard-smoke R2 objects."""

from __future__ import annotations

import argparse
import hashlib
import json
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


def _query_router(path: Path) -> dict[str, Any]:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = db.execute(
            """
            SELECT token, shard_id, max_importance
            FROM router
            WHERE token = 'monaco' AND shard_id = 'MC'
            ORDER BY max_importance DESC
            LIMIT 1
            """
        ).fetchone()
    finally:
        db.close()
    if row is None:
        raise RuntimeError("fresh router has no monaco -> MC route")
    return {"token": row[0], "shard_id": row[1], "importance": row[2]}


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
        "router": _query_router(router_db),
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
