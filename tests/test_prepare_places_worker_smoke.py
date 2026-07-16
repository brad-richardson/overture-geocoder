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
        rows.append(
            {
                "id": f"{region}-{index}",
                "name": "東京タワー" if cjk and index == 0 else "Shared Cafe",
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
    assert all(case["result_ids"] for case in report["cases"])
