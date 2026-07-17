#!/usr/bin/env python3
"""Build three compact Places shards, one packed head, and exact smoke oracles."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from experiment_places_compact_index import load_places  # noqa: E402
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


def has_cjk(value: str) -> bool:
    return any(
        0x3400 <= ord(character) <= 0x4DBF
        or 0x4E00 <= ord(character) <= 0x9FFF
        or 0x3040 <= ord(character) <= 0x30FF
        or 0x31F0 <= ord(character) <= 0x31FF
        or 0xAC00 <= ord(character) <= 0xD7AF
        for character in value
    )


def query_shards(artifacts: list[Path], token: str, prefix: bool) -> dict[str, Any]:
    results = []
    candidates = 0
    for artifact in artifacts:
        result = CompactShard(artifact).query(
            QueryCase("smoke", (Clause(token, prefix=prefix),), "smoke")
        )
        candidates += result["candidate_count"]
        results.extend(result["results"])
    # Python's stable sort preserves shard-major/local-doc order for equal
    # quantized confidence, matching both per-shard truncation and the Worker.
    results.sort(key=lambda row: -row["confidence"])
    return {
        "candidate_count": candidates,
        "result_ids": [row["id"] for row in results[:10]],
    }


def prepare(
    inputs: list[Path], output_dir: Path, *, head_minimum_candidates: int = 64
) -> dict[str, Any]:
    if len(inputs) != 3:
        raise ValueError("the Places Worker smoke requires exactly three inputs")
    output_dir.mkdir(parents=True, exist_ok=True)
    artifacts = []
    ordered_groups = []
    builds = []
    for index, input_path in enumerate(inputs):
        places = load_places(input_path)
        artifact = output_dir / f"shard-{index}.pcsh"
        ordered, report = build_artifact(places, artifact)
        artifacts.append(artifact)
        ordered_groups.append(ordered)
        builds.append(report)

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
        offset, length = head_index[key]
        entries_base, _ = head_reader.component("entries")
        records = decode_head_entry(head_reader._read(entries_base + offset, length))
        cases.append(
            {
                "name": "head_exact",
                "token": key[2:],
                "prefix": False,
                "head_hit": True,
                "candidate_count": len(records),
                "result_ids": [record["id"] for record in records],
            }
        )

    token_counts: dict[str, int] = {}
    for group in ordered_groups:
        for token, docs in posting_map(group).items():
            token_counts[token] = token_counts.get(token, 0) + len(docs)
    fallback_tokens = [
        token
        for token, count in sorted(
            token_counts.items(), key=lambda item: (item[1], item[0])
        )
        if f"e:{token}" not in head_index and 1 <= count <= 10_000
    ]
    if not fallback_tokens:
        raise ValueError("no bounded non-head token exists in the three shards")
    exact = fallback_tokens[0]
    exact_result = query_shards(artifacts, exact, False)
    cases.append(
        {
            "name": "shard_exact",
            "token": exact,
            "prefix": False,
            "head_hit": False,
            **exact_result,
        }
    )

    prefix_case = None
    for token in fallback_tokens:
        if len(token) < 4 or has_cjk(token):
            continue
        # Prefer a proper last-token prefix, but the full token through the
        # prefix path is still a valid bounded fallback when every shorter
        # prefix was promoted into the packed head.
        lengths = [*range(len(token) - 1, 1, -1), len(token)]
        for length in lengths:
            prefix = token[:length]
            if f"p:{prefix}" in head_index:
                continue
            result = query_shards(artifacts, prefix, True)
            if 0 < result["candidate_count"] <= 10_000:
                prefix_case = {
                    "name": "shard_prefix",
                    "token": prefix,
                    "prefix": True,
                    "head_hit": False,
                    **result,
                }
                break
        if prefix_case is not None:
            break
    if prefix_case is None:
        raise ValueError("no bounded non-head prefix case exists in the three shards")
    cases.append(prefix_case)

    cjk_case = None
    for token in fallback_tokens:
        if not has_cjk(token):
            continue
        result = query_shards(artifacts, token, False)
        if result["candidate_count"]:
            cjk_case = {
                "name": "cjk_exact",
                "token": token,
                "prefix": False,
                "head_hit": False,
                **result,
            }
            break
    if cjk_case:
        cases.append(cjk_case)

    return {
        "schema": "overture-places-worker-fixture-v1",
        "inputs": [str(path) for path in inputs],
        "shards": builds,
        "head": head_report,
        "cases": cases,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", action="append", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--head-minimum-candidates", type=int, default=64)
    args = parser.parse_args()
    report = prepare(
        args.input,
        args.output_dir,
        head_minimum_candidates=args.head_minimum_candidates,
    )
    (args.output_dir / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
