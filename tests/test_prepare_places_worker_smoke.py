from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "prepare_places_worker_smoke.py"
SPEC = importlib.util.spec_from_file_location("prepare_places_worker_smoke", SCRIPT)
assert SPEC and SPEC.loader
smoke = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = smoke
SPEC.loader.exec_module(smoke)


def write_places(path: Path, region: str, *, cjk: bool = False) -> None:
    rows = []
    for index in range(8):
        name = "Shared Cafe"
        if index == 1:
            name = f"Unique{region}"
        if cjk and index == 0:
            name = "東京タワー"
        rows.append(
            {
                "id": f"{region}-{index}",
                "name": name,
                "category": "cafe",
                "region": region,
                "country": "US",
                "lat": 40 + index / 100,
                "lon": -70 - index / 100,
                "confidence": 0.9 - index / 100,
            }
        )
    path.write_text(json.dumps(rows))


def test_builds_three_shards_head_and_exact_oracles(tmp_path):
    inputs = [tmp_path / f"input-{index}.json" for index in range(3)]
    for index, path in enumerate(inputs):
        write_places(path, f"R{index}", cjk=index == 2)

    report = smoke.prepare(inputs, tmp_path / "output", head_minimum_candidates=2)

    assert len(report["shards"]) == 3
    assert (tmp_path / "output" / "head.phrp").is_file()
    assert any(case["head_hit"] for case in report["cases"])
    assert any(not case["head_hit"] for case in report["cases"])
    assert any(case["name"] == "shard_prefix" for case in report["cases"])
    assert all(case["result_ids"] for case in report["cases"])


def test_equal_rank_global_merge_uses_same_shard_doc_tiebreak_as_local_limit(tmp_path):
    inputs = [tmp_path / f"input-{index}.json" for index in range(3)]
    first = [
        {
            "id": f"z{index:02}",
            "name": "Tie Place",
            "category": "cafe",
            "lat": -80.0,
            "lon": -170.0,
            "confidence": 0.9,
        }
        for index in range(10)
    ]
    first.append(
        {
            "id": "a-local-late-cell",
            "name": "Tie Place",
            "category": "cafe",
            "lat": 80.0,
            "lon": 170.0,
            "confidence": 0.9,
        }
    )
    inputs[0].write_text(json.dumps(first))
    inputs[1].write_text(
        json.dumps(
            [
                {
                    "id": "a-next-shard",
                    "name": "Tie Place",
                    "category": "cafe",
                    "lat": 0.0,
                    "lon": 0.0,
                    "confidence": 0.9,
                }
            ]
        )
    )
    inputs[2].write_text(
        json.dumps(
            [
                {
                    "id": "unrelated",
                    "name": "Other",
                    "category": "shop",
                    "lat": 0.0,
                    "lon": 0.0,
                    "confidence": 0.1,
                }
            ]
        )
    )
    artifacts = []
    ordered_groups = []
    for index, path in enumerate(inputs):
        artifact = tmp_path / f"shard-{index}.pcsh"
        ordered, _ = smoke.build_artifact(smoke.load_places(path), artifact)
        artifacts.append(artifact)
        ordered_groups.append(ordered)

    result = smoke.query_shards(artifacts, "tie", False)
    head_order, heads, _ = smoke.build_heads_and_baseline(
        [place for group in ordered_groups for place in group],
        head_minimum_candidates=2,
        preserve_input_order=True,
    )
    head_path = tmp_path / "head.phrp"
    smoke.build_repack_object(head_order, heads, head_path)
    head_reader = smoke.RepackHead(head_path)
    offset, length = head_reader.load_resident_index()["e:tie"]
    entries_base, _ = head_reader.component("entries")
    head_ids = [
        row["id"]
        for row in smoke.decode_head_entry(
            head_reader._read(entries_base + offset, length)
        )
    ]

    assert result["candidate_count"] == 12
    assert result["result_ids"] == [f"z{index:02}" for index in range(10)]
    assert "a-next-shard" not in result["result_ids"]
    assert head_ids == result["result_ids"]
