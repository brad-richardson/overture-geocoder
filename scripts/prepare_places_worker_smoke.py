#!/usr/bin/env python3
"""Build routed compact Places shards, a packed head, and exact smoke oracles."""

from __future__ import annotations

import argparse
import json
import math
import struct
import sys
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from experiment_places_compact_index import load_places, normalize  # noqa: E402
from experiment_places_compact_shard import (  # noqa: E402
    Clause,
    CompactShard,
    QueryCase,
    build_artifact,
    posting_map,
)
from experiment_places_head_repack import (  # noqa: E402
    build_heads_and_baseline,
    build_repack_object,
    decode_head_entry,
    RepackHead,
)


CATALOG_MAGIC = b"PCAT0001"
CATALOG_PREAMBLE = struct.Struct("<8sI")
TOKENIZER_VERSION = "nfkd-latin-fold-cjk-bigram-v2"
RESULT_LIMIT = 10
SHARD_FETCH_LIMIT = 25


def response_projection(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "latitude": row.get("latitude", row.get("lat")),
        "longitude": row.get("longitude", row.get("lon")),
        "confidence": row["confidence"],
        "name": row["name"],
        "category": row["category"],
        "locality": row["locality"],
        "region": row["region"],
        "country": row["country"],
        **({"distance_km": row["distance_km"]} if "distance_km" in row else {}),
    }


def has_cjk(value: str) -> bool:
    return any(
        0x3400 <= ord(character) <= 0x4DBF
        or 0x4E00 <= ord(character) <= 0x9FFF
        or 0x3040 <= ord(character) <= 0x30FF
        or 0x31F0 <= ord(character) <= 0x31FF
        or 0xAC00 <= ord(character) <= 0xD7AF
        for character in value
    )


def haversine_km(
    latitude_a: float, longitude_a: float, latitude_b: float, longitude_b: float
) -> float:
    radius_km = 6371.0088
    delta_latitude = math.radians(latitude_b - latitude_a)
    delta_longitude = math.radians(longitude_b - longitude_a)
    latitude_a = math.radians(latitude_a)
    latitude_b = math.radians(latitude_b)
    haversine = math.sin(delta_latitude / 2) ** 2 + math.cos(
        latitude_a
    ) * math.cos(latitude_b) * math.sin(delta_longitude / 2) ** 2
    return 2 * radius_km * math.asin(math.sqrt(haversine))


def shard_route(identifier: str, object_name: str, places: list[Any]) -> dict[str, Any]:
    if not places:
        raise ValueError("a routed Places shard cannot be empty")
    xmin = min(place.lon for place in places)
    ymin = min(place.lat for place in places)
    xmax = max(place.lon for place in places)
    ymax = max(place.lat for place in places)
    return {
        "id": identifier,
        "object": object_name,
        "bbox": [xmin, ymin, xmax, ymax],
        "center": [(xmin + xmax) / 2, (ymin + ymax) / 2],
    }


def build_catalog(routes: list[dict[str, Any]], output: Path) -> dict[str, Any]:
    payload = {
        "schema_version": 1,
        "tokenizer_version": TOKENIZER_VERSION,
        "shards": routes,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    output.write_bytes(CATALOG_PREAMBLE.pack(CATALOG_MAGIC, len(encoded)) + encoded)
    return {
        "schema_version": 1,
        "object": output.name,
        "bytes": output.stat().st_size,
        "shards": routes,
    }


def query_shard(
    artifact: Path, clauses: tuple[Clause, ...], center: list[float]
) -> dict[str, Any]:
    result = CompactShard(artifact).query(
        QueryCase("smoke", clauses, "smoke"), limit=SHARD_FETCH_LIMIT
    )
    rows = result["results"]
    for row in rows:
        row["latitude"] = row.pop("lat")
        row["longitude"] = row.pop("lon")
        row["distance_km"] = haversine_km(
            center[1], center[0], row["latitude"], row["longitude"]
        )
    # Distance is retained for human scoring but is not used to reorder the
    # reader's bounded confidence/doc-ID result window.
    projected = [response_projection(row) for row in rows[:RESULT_LIMIT]]
    return {
        "candidate_count": result["candidate_count"],
        "clause_candidate_counts": result["clause_candidate_counts"],
        "result_ids": [row["id"] for row in projected],
        "results": projected,
    }


def query_head(reader: RepackHead, clauses: tuple[Clause, ...]) -> dict[str, Any]:
    if not clauses or len(clauses) > 2 or any(
        clause.field is not None or clause.prefix for clause in clauses
    ):
        return {"head_hit": False, "candidate_count": 0, "result_ids": [], "results": []}
    index = reader.load_resident_index()
    entries_base, _ = reader.component("entries")
    per_clause = []
    for clause in clauses:
        located = index.get(f"e:{normalize(clause.value)}")
        if located is None:
            return {
                "head_hit": False,
                "candidate_count": 0,
                "result_ids": [],
                "results": [],
            }
        offset, length = located
        per_clause.append(
            decode_head_entry(reader._read(entries_base + offset, length))
        )
    rows = per_clause[0]
    for other in per_clause[1:]:
        ids = {row["id"] for row in other}
        rows = [row for row in rows if row["id"] in ids]
    rows = [response_projection(row) for row in rows[:RESULT_LIMIT]]
    return {
        "head_hit": bool(rows),
        "candidate_count": len(rows),
        "result_ids": [row["id"] for row in rows],
        "results": rows,
    }


def clause_json(clause: Clause) -> dict[str, Any]:
    return {
        "token": normalize(clause.value),
        "prefix": clause.prefix,
        "field": clause.field,
    }


def routed_case(
    *,
    name: str,
    context: str,
    shard_index: int,
    clauses: tuple[Clause, ...],
    artifacts: list[Path],
    routes: list[dict[str, Any]],
    case_class: str = "reader_equivalence",
    query: str | None = None,
    relevant_if: str | None = None,
    point_route: bool = False,
) -> dict[str, Any]:
    expected = query_shard(artifacts[shard_index], clauses, routes[shard_index]["center"])
    return {
        "name": name,
        "scope": name.replace("_", "-"),
        "class": case_class,
        "query": query,
        "context": None if point_route else context,
        "point": routes[shard_index]["center"] if point_route else None,
        "clauses": [clause_json(clause) for clause in clauses],
        "route": "catalog_point" if point_route else "catalog_context",
        "route_shard": routes[shard_index]["id"],
        "head_hit": False,
        "required_objects": ["catalog.pcat", routes[shard_index]["object"]],
        "relevant_if": relevant_if,
        **expected,
    }


def head_case(
    *,
    name: str,
    clauses: tuple[Clause, ...],
    reader: RepackHead,
    case_class: str = "reader_equivalence",
    query: str | None = None,
    relevant_if: str | None = None,
) -> dict[str, Any]:
    expected = query_head(reader, clauses)
    return {
        "name": name,
        "scope": name.replace("_", "-"),
        "class": case_class,
        "query": query,
        "context": None,
        "clauses": [clause_json(clause) for clause in clauses],
        "route": "packed_head" if expected["head_hit"] else "head_only_miss",
        "route_shard": None,
        "required_objects": ["head.phrp"],
        "relevant_if": relevant_if,
        **expected,
    }


def relevance_cases(
    seed_path: Path,
    *,
    contexts: list[str],
    artifacts: list[Path],
    routes: list[dict[str, Any]],
    head_reader: RepackHead,
) -> list[dict[str, Any]]:
    seed = json.loads(seed_path.read_text())
    if seed.get("schema") != "overture-places-relevance-seed-v1":
        raise ValueError("unsupported Places relevance seed")
    context_index = {name: index for index, name in enumerate(contexts)}
    required_contexts = {"boston", "tokyo", "mexico-city"}
    if not required_contexts.issubset(context_index):
        return []
    definitions = {
        "brand_with_context": ("boston", (Clause("starbucks"),)),
        "local_name": ("tokyo", (Clause("東京タワー", field="name"),)),
        "category_near_me": (
            "mexico-city",
            (Clause("cafe", field="category"),),
        ),
        "ambiguous_context": ("boston", (Clause("cambridge", field="name"),)),
        "famous_unique": (None, (Clause("tokyo"), Clause("tower"))),
        "chain_name": ("tokyo", (Clause("7"), Clause("eleven"))),
    }
    cases = []
    for label in seed["cases"]:
        case_class = label["class"]
        context, clauses = definitions[case_class]
        name = f"relevance_{case_class}"
        if context is None:
            case = head_case(
                name=name,
                clauses=clauses,
                reader=head_reader,
                case_class=case_class,
                query=label["query"],
                relevant_if=label["relevant_if"],
            )
        else:
            shard_index = context_index[context]
            case = routed_case(
                name=name,
                context=context,
                shard_index=shard_index,
                clauses=clauses,
                artifacts=artifacts,
                routes=routes,
                case_class=case_class,
                query=label["query"],
                relevant_if=label["relevant_if"],
            )
        cases.append(case)
    return cases


def prepare(
    inputs: list[Path],
    output_dir: Path,
    *,
    contexts: list[str] | None = None,
    head_minimum_candidates: int = 64,
    relevance_seed: Path | None = None,
) -> dict[str, Any]:
    if len(inputs) != 3:
        raise ValueError("the Places Worker smoke requires exactly three inputs")
    contexts = contexts or [f"shard-{index}" for index in range(len(inputs))]
    if len(contexts) != len(inputs) or len(set(contexts)) != len(contexts):
        raise ValueError("Places smoke contexts must be unique and align with inputs")
    if any(
        not value
        or len(value) > 64
        or any(
            not (character.isascii() and (character.isalnum() or character == "-"))
            for character in value
        )
        for value in contexts
    ):
        raise ValueError("Places smoke context IDs are outside hard bounds")
    output_dir.mkdir(parents=True, exist_ok=True)
    artifacts = []
    ordered_groups = []
    builds = []
    routes = []
    for index, (input_path, context) in enumerate(zip(inputs, contexts)):
        places = load_places(input_path)
        artifact = output_dir / f"shard-{index}.pcsh"
        ordered, report = build_artifact(places, artifact)
        route = shard_route(context, artifact.name, ordered)
        artifacts.append(artifact)
        ordered_groups.append(ordered)
        routes.append(route)
        builds.append({**report, "route": route})

    catalog_report = build_catalog(routes, output_dir / "catalog.pcat")
    combined = [place for group in ordered_groups for place in group]
    ordered, heads, _ = build_heads_and_baseline(
        combined,
        head_minimum_candidates=head_minimum_candidates,
        preserve_input_order=True,
    )
    head_path = output_dir / "head.phrp"
    head_report = build_repack_object(ordered, heads, head_path)
    head_reader = RepackHead(head_path)
    head_index = head_reader.load_resident_index()

    cases = []
    exact_head_keys = sorted(key for key in head_index if key.startswith("e:"))
    if exact_head_keys:
        preferred = "e:starbucks"
        key = preferred if preferred in head_index else exact_head_keys[0]
        cases.append(
            head_case(
                name="head_exact",
                clauses=(Clause(key[2:]),),
                reader=head_reader,
            )
        )

    fallback_by_shard: list[list[tuple[str, int]]] = []
    for group in ordered_groups:
        fallback_by_shard.append(
            sorted(
                (
                    (token, len(docs))
                    for token, docs in posting_map(group).items()
                    if f"e:{token}" not in head_index and 1 <= len(docs) <= 10_000
                ),
                key=lambda item: (item[1], item[0]),
            )
        )
    selected = next(
        (
            (index, candidates[0][0])
            for index, candidates in enumerate(fallback_by_shard)
            if candidates
        ),
        None,
    )
    if selected is None:
        raise ValueError("no bounded non-head token exists in the three shards")
    shard_index, exact = selected
    cases.append(
        routed_case(
            name="shard_exact",
            context=contexts[shard_index],
            shard_index=shard_index,
            clauses=(Clause(exact),),
            artifacts=artifacts,
            routes=routes,
            point_route=True,
        )
    )

    prefix_case = None
    for shard_index, candidates in enumerate(fallback_by_shard):
        for token, _ in candidates:
            if len(token) < 4 or has_cjk(token):
                continue
            lengths = [*range(len(token) - 1, 1, -1), len(token)]
            for length in lengths:
                prefix = token[:length]
                result = query_shard(
                    artifacts[shard_index],
                    (Clause(prefix, prefix=True),),
                    routes[shard_index]["center"],
                )
                if 0 < result["candidate_count"] <= 10_000:
                    prefix_case = routed_case(
                        name="shard_prefix",
                        context=contexts[shard_index],
                        shard_index=shard_index,
                        clauses=(Clause(prefix, prefix=True),),
                        artifacts=artifacts,
                        routes=routes,
                    )
                    break
            if prefix_case is not None:
                break
        if prefix_case is not None:
            break
    if prefix_case is None:
        raise ValueError("no bounded routed prefix case exists in the three shards")
    cases.append(prefix_case)

    cjk_case = None
    for shard_index, candidates in enumerate(fallback_by_shard):
        for token, _ in candidates:
            if has_cjk(token):
                cjk_case = routed_case(
                    name="cjk_exact",
                    context=contexts[shard_index],
                    shard_index=shard_index,
                    clauses=(Clause(token),),
                    artifacts=artifacts,
                    routes=routes,
                )
                break
        if cjk_case is not None:
            break
    if cjk_case:
        cases.append(cjk_case)

    seed_path = relevance_seed or SCRIPT_DIR.parent / "benchmarks" / "places-relevance-seed.json"
    cases.extend(
        relevance_cases(
            seed_path,
            contexts=contexts,
            artifacts=artifacts,
            routes=routes,
            head_reader=head_reader,
        )
    )
    return {
        "schema": "overture-places-worker-fixture-v2",
        "inputs": [str(path) for path in inputs],
        "contexts": contexts,
        "shards": builds,
        "catalog": catalog_report,
        "head": {
            **head_report,
            "eligibility": "context-free, one-or-two exact unfielded tokens; all packed top-10 entries must exist and have a non-empty ID intersection",
        },
        "cases": cases,
        "failure_cases": [
            {
                "name": "unknown_context",
                "scope": "unknown-context",
                "context": "not-a-catalog-context",
                "clauses": [{"token": "cafe", "prefix": False, "field": None}],
                "expected_status": 400,
                "expected_route": "catalog_miss",
                "required_objects": ["catalog.pcat"],
            }
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", action="append", type=Path, required=True)
    parser.add_argument("--context", action="append")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--head-minimum-candidates", type=int, default=64)
    parser.add_argument("--relevance-seed", type=Path)
    args = parser.parse_args()
    report = prepare(
        args.input,
        args.output_dir,
        contexts=args.context,
        head_minimum_candidates=args.head_minimum_candidates,
        relevance_seed=args.relevance_seed,
    )
    (args.output_dir / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
