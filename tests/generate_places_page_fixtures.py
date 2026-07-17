#!/usr/bin/env python3
"""Generate compact Places shard/head fixtures consumed by Rust tests."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from experiment_places_compact_index import Place  # noqa: E402
from experiment_places_compact_shard import build_artifact  # noqa: E402
from experiment_places_head_repack import (  # noqa: E402
    build_heads_and_baseline,
    build_repack_object,
)


FIXTURE_DIR = ROOT / "tests" / "fixtures" / "places-pages"


def places() -> list[Place]:
    return [
        Place(
            place_id=f"fixture-{index:02}",
            name="Shared Cafe",
            brand="Fixture Brand",
            category="cafe",
            locality="Boston",
            region="MA",
            country="US",
            lat=42.35 + index / 10_000,
            lon=-71.10,
            confidence=0.95 - index / 100,
        )
        for index in range(12)
    ]


def write(output_dir: Path) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    source = places()
    shard = output_dir / "shard.pcsh"
    ordered_shard, _ = build_artifact(source, shard)
    ordered_head, heads, _ = build_heads_and_baseline(source, head_minimum_candidates=2)
    head = output_dir / "head.phrp"
    build_repack_object(ordered_head, heads, head)
    files = {path.name: path.read_bytes() for path in (shard, head)}
    report = {
        "schema": "overture-places-page-fixture-v1",
        "token": "shared",
        "shard_first_id": ordered_shard[0].place_id,
        "head_result_ids": [
            ordered_head[doc_id].place_id for doc_id in heads["e:shared"]
        ],
        "files": {
            name: {
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
            for name, payload in sorted(files.items())
        },
    }
    (output_dir / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n"
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=FIXTURE_DIR)
    args = parser.parse_args()
    print(json.dumps(write(args.output_dir), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
