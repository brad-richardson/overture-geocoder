#!/usr/bin/env python3
"""Collect direct Foursquare-to-Wikidata sidecar inputs from public data.

The Wikidata snapshot is a frozen SPARQL result containing P1968 claims.  This
collector joins those claims to Overture's public Foursquare bridge, then reads
the matching public Places rows for the names and coordinates an independent
reviewer needs.  It emits normalized JSONL inputs for ``sidecar_phase0.py`` and
a source-hashed report; it does not make a matching decision itself.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any


SPARQL_QUERY = """SELECT ?item ?foursquare ?coord ?itemLabel WHERE {
  ?item wdt:P1968 ?foursquare .
  OPTIONAL { ?item wdt:P625 ?coord }
  SERVICE wikibase:label {
    bd:serviceParam wikibase:language "en,[AUTO_LANGUAGE]".
  }
} LIMIT 1000"""
QID_URL_RE = re.compile(r"^https?://www\.wikidata\.org/entity/(Q[1-9][0-9]*)$")
POINT_RE = re.compile(
    r"^Point\((-?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)) "
    r"(-?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+))\)$"
)
RELEASE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}\.[0-9]+$")


def canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _binding_value(binding: dict[str, Any], key: str) -> str | None:
    value = binding.get(key)
    if not isinstance(value, dict) or value.get("value") in (None, ""):
        return None
    return str(value["value"])


def parse_point(value: str | None) -> tuple[float, float] | None:
    if value is None:
        return None
    match = POINT_RE.fullmatch(value)
    if match is None:
        raise ValueError(f"unsupported Wikidata coordinate: {value!r}")
    longitude, latitude = map(float, match.groups())
    if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
        raise ValueError(f"Wikidata coordinate is out of range: {value!r}")
    return latitude, longitude


def parse_sparql_snapshot(payload: dict[str, Any]) -> list[dict[str, Any]]:
    bindings = (payload.get("results") or {}).get("bindings")
    if not isinstance(bindings, list):
        raise ValueError("Wikidata snapshot omits results.bindings")
    grouped: dict[str, dict[str, Any]] = {}
    for index, binding in enumerate(bindings):
        if not isinstance(binding, dict):
            raise ValueError(f"Wikidata binding {index} is not an object")
        item = _binding_value(binding, "item")
        match = QID_URL_RE.fullmatch(item or "")
        external_id = _binding_value(binding, "foursquare")
        label = _binding_value(binding, "itemLabel")
        if match is None or not external_id or not label:
            raise ValueError(f"Wikidata binding {index} lacks QID, P1968 ID, or label")
        qid = match.group(1)
        coordinate = parse_point(_binding_value(binding, "coord"))
        row = grouped.setdefault(qid, {
            "wikidata_qid": qid,
            "names": set(),
            "coordinates": set(),
            "external_ids": set(),
        })
        row["names"].add(label)
        row["external_ids"].add(external_id)
        if coordinate is not None:
            row["coordinates"].add(coordinate)

    result = []
    for qid, row in grouped.items():
        coordinates = sorted(row["coordinates"])
        # Multiple P625 statements are legitimate source evidence, not a reason
        # to select one arbitrarily.  Direct-ID acceptance does not use distance;
        # retain every point for review and expose a singular coordinate only
        # when the snapshot itself is unambiguous.
        coordinate = coordinates[0] if len(coordinates) == 1 else None
        result.append({
            "wikidata_qid": qid,
            "names": sorted(row["names"]),
            "latitude": coordinate[0] if coordinate else None,
            "longitude": coordinate[1] if coordinate else None,
            "coordinate_candidates": [
                {"latitude": latitude, "longitude": longitude}
                for latitude, longitude in coordinates
            ],
            "external_ids": {"Foursquare": sorted(row["external_ids"])},
        })
    return sorted(result, key=lambda row: int(row["wikidata_qid"][1:]))


def bridge_glob(release: str) -> str:
    if not RELEASE_RE.fullmatch(release):
        raise ValueError(f"invalid Overture release: {release!r}")
    return (
        "s3://overturemaps-us-west-2/bridgefiles/"
        f"{release}/dataset=Foursquare/theme=places/type=place/*"
    )


def places_glob(release: str) -> str:
    if not RELEASE_RE.fullmatch(release):
        raise ValueError(f"invalid Overture release: {release!r}")
    return (
        "s3://overturemaps-us-west-2/release/"
        f"{release}/theme=places/type=place/*"
    )


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f".{path.name}.tmp")
    with staged.open("wb") as handle:
        for row in rows:
            handle.write(canonical_json(row))
    staged.replace(path)


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f".{path.name}.tmp")
    staged.write_bytes(canonical_json(value))
    staged.replace(path)


def collect(
    *,
    snapshot_path: Path,
    release: str,
    threads: int,
    memory_limit: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    try:
        import duckdb
        import pyarrow as pa
    except ImportError as error:  # pragma: no cover - dependency failure
        raise RuntimeError("duckdb and pyarrow are required for collection") from error
    try:
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"Wikidata snapshot is not readable JSON: {snapshot_path}") from error
    entities = parse_sparql_snapshot(snapshot)
    claims = [
        (external_id, entity["wikidata_qid"])
        for entity in entities
        for external_id in entity["external_ids"]["Foursquare"]
    ]
    connection = duckdb.connect()
    connection.execute(f"SET threads = {threads}")
    connection.execute("SET memory_limit = ?", [memory_limit])
    connection.execute("SET enable_progress_bar = false")
    connection.register("direct_claims", pa.table({
        "record_id": [row[0] for row in claims],
        "wikidata_qid": [row[1] for row in claims],
    }))
    bridge_rows = connection.execute(
        """
        SELECT DISTINCT cast(b.id AS VARCHAR) AS gers_id,
               b.record_id, d.wikidata_qid
        FROM read_parquet(?, hive_partitioning=true) b
        JOIN direct_claims d USING (record_id)
        ORDER BY gers_id, wikidata_qid, record_id
        """,
        [bridge_glob(release)],
    ).fetchall()
    if not bridge_rows:
        raise ValueError("no Wikidata P1968 claims joined the Overture bridge")
    matched_ids = sorted({row[0] for row in bridge_rows})
    connection.register("matched_gers", pa.table({"gers_id": matched_ids}))
    place_rows = connection.execute(
        """
        SELECT cast(p.id AS VARCHAR) AS gers_id,
               p.names.primary AS primary_name,
               map_values(p.names.common) AS common_names,
               p.bbox.ymin AS latitude,
               p.bbox.xmin AS longitude,
               (p.addresses[1]).country AS country
        FROM read_parquet(?, union_by_name=true, hive_partitioning=true) p
        JOIN matched_gers m ON cast(p.id AS VARCHAR) = m.gers_id
        ORDER BY gers_id
        """,
        [places_glob(release)],
    ).fetchall()
    version = connection.execute("SELECT version()").fetchone()[0]
    connection.close()
    if len(place_rows) != len(matched_ids):
        found = {row[0] for row in place_rows}
        missing = sorted(set(matched_ids) - found)
        raise ValueError(
            f"Places extraction returned {len(place_rows)} rows for "
            f"{len(matched_ids)} GERS IDs; first missing: {missing[:8]}"
        )

    sources_by_gers: dict[str, set[tuple[str, str]]] = defaultdict(set)
    joined_qids = set()
    source_owners: dict[str, set[str]] = defaultdict(set)
    for gers_id, record_id, qid in bridge_rows:
        sources_by_gers[gers_id].add(("Foursquare", record_id))
        source_owners[record_id].add(gers_id)
        joined_qids.add(qid)
    normalized_places = []
    for gers_id, primary_name, common_names, latitude, longitude, country in place_rows:
        names = sorted({
            str(value).strip()
            for value in [primary_name, *(common_names or [])]
            if value is not None and str(value).strip()
        })
        if not names:
            raise ValueError(f"Overture place {gers_id} has no reviewable name")
        normalized_places.append({
            "gers_id": gers_id,
            "names": names,
            "latitude": latitude,
            "longitude": longitude,
            "country": country,
            "sources": [
                {"dataset": dataset, "record_id": record_id}
                for dataset, record_id in sorted(sources_by_gers[gers_id])
            ],
        })
    normalized_entities = [
        entity for entity in entities if entity["wikidata_qid"] in joined_qids
    ]
    ambiguous_source_ids = sorted(
        record_id for record_id, owners in source_owners.items() if len(owners) != 1
    )
    report = {
        "schema": "gers-qid-sidecar-foursquare-collection-v1",
        "meta": {
            "overture_release": release,
            "wikidata_property": "P1968",
            "wikidata_property_label": "Foursquare City Guide venue ID",
            "wikidata_sparql_endpoint": "https://query.wikidata.org/sparql",
            "wikidata_query": SPARQL_QUERY,
            "wikidata_query_sha256": hashlib.sha256(SPARQL_QUERY.encode()).hexdigest(),
            "wikidata_snapshot_sha256": sha256_file(snapshot_path),
            "overture_bridge": bridge_glob(release),
            "overture_places": places_glob(release),
            "duckdb_version": version,
            "construction_contract_movement": False,
        },
        "counts": {
            "wikidata_bindings": len((snapshot.get("results") or {}).get("bindings", [])),
            "wikidata_entities": len(entities),
            "direct_external_id_claims": len(claims),
            "joined_bridge_rows": len(bridge_rows),
            "joined_gers_ids": len(normalized_places),
            "joined_wikidata_qids": len(normalized_entities),
            "wikidata_entities_with_multiple_coordinates": sum(
                len(entity["coordinate_candidates"]) > 1
                for entity in normalized_entities
            ),
            "source_record_ids_with_multiple_gers": len(ambiguous_source_ids),
        },
        "ambiguous_source_record_ids": ambiguous_source_ids,
    }
    return normalized_places, normalized_entities, report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wikidata-snapshot", type=Path, required=True)
    parser.add_argument("--overture-release", default="2026-06-17.0")
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--memory-limit", default="8GB")
    parser.add_argument("--places-output", type=Path, required=True)
    parser.add_argument("--entities-output", type=Path, required=True)
    parser.add_argument("--report-output", type=Path, required=True)
    args = parser.parse_args(argv)
    if not 1 <= args.threads <= 64:
        parser.error("--threads must be between 1 and 64")
    if not re.fullmatch(r"[1-9][0-9]{0,4}(?:MB|GB)", args.memory_limit):
        parser.error("--memory-limit must be an integer MB or GB value")
    try:
        places, entities, report = collect(
            snapshot_path=args.wikidata_snapshot,
            release=args.overture_release,
            threads=args.threads,
            memory_limit=args.memory_limit,
        )
        _write_jsonl(args.places_output, places)
        _write_jsonl(args.entities_output, entities)
        report["outputs"] = {
            "places_sha256": sha256_file(args.places_output),
            "entities_sha256": sha256_file(args.entities_output),
        }
        _write_json(args.report_output, report)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"sidecar Foursquare collection failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(report["counts"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
