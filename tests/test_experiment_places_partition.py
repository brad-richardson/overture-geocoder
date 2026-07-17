"""Tests for the bbox extractor and the partition-comparison driver."""

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# --- bbox extractor: determinism + projection parity (source inspection) ----


def test_partition_extractor_is_deterministic_and_bbox_parameterized():
    source = (SCRIPTS / "experiment_places_partition_extract.py").read_text()
    # Prominence-ordered (confidence DESC) with an id tiebreak so the sample
    # keeps landmarks the relevance seeds query instead of the smallest UUIDs,
    # while staying deterministic via the tiebreak + preserved insertion order.
    order_by = "ORDER BY COALESCE(confidence, 0.5) DESC, id"
    assert order_by in source
    assert "LIMIT {args.limit}" in source
    assert "preserve_insertion_order=true" in source
    assert "preserve_insertion_order=false" not in source
    assert source.index(order_by) < source.index("LIMIT {args.limit}")
    # bbox is parameterized, not hard-coded to California.
    assert "bbox.xmin BETWEEN {xmin} AND {xmax}" in source
    # Alternate/common names are projected for cross-lingual name matching.
    assert "AS alt_names" in source


def test_partition_extractor_projection_matches_factory():
    """The parameterized extractor must project the same columns as the pinned one."""
    factory = (SCRIPTS / "factory_extract_places.py").read_text()
    partition = _load("experiment_places_partition_extract")
    for column in (
        "id AS gers_id",
        "names.primary AS primary_name",
        "COALESCE(brand.names.primary, '') AS brand_name",
        "COALESCE(categories.primary, basic_category, '') AS category_primary",
        "COALESCE(addresses[1].locality, '') AS locality",
        "COALESCE(addresses[1].region, '') AS region",
        "COALESCE(addresses[1].country, '') AS country",
        "ST_Y(geometry) AS lat",
        "ST_X(geometry) AS lon",
        "COALESCE(confidence, 0.5) AS confidence",
    ):
        assert column in factory
        assert column in partition.PROJECTION


# --- partition-comparison driver: pure classification + end-to-end ----------


compare = _load("experiment_places_partition_compare")


def test_token_script_classification():
    assert compare.token_script("東京") == "han"
    assert compare.token_script("あさひ") == "kana"
    assert compare.token_script("スシ") == "kana"
    assert compare.token_script("cafe") == "latin"
    assert compare.token_script("123") == "digit"


def test_script_mix_proportions_sum_to_one():
    freq = {"cafe": 5, "東京": 3, "スシ": 2, "hotel": 4}
    mix = compare.script_mix(freq)
    total = sum(v["token_proportion"] for v in mix["by_script"].values())
    assert abs(total - 1.0) < 1e-9
    assert mix["cjk_dominant_tokens"] == 2  # 東京 (han) + スシ (kana)


def _write_jsonl(path, rows):
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows))


def test_compare_driver_end_to_end(tmp_path):
    ca_rows = [
        {"id": f"ca-{i}", "name": n, "category": "cafe", "confidence": 0.9 - i / 100,
         "lat": 37.7 + i / 1000, "lon": -122.4}
        for i, n in enumerate(
            ["Blue Bottle Coffee", "Golden Gate Cafe", "Harbor Hotel", "Ferry Market"]
        )
    ]
    jp_rows = [
        {"id": f"jp-{i}", "name": n, "category": "cafe", "confidence": 0.9 - i / 100,
         "lat": 35.6 + i / 1000, "lon": 139.7}
        for i, n in enumerate(["東京駅前カフェ", "渋谷スシ", "Tokyo Tower Cafe", "新宿ホテル"])
    ]
    ca = tmp_path / "ca.jsonl"
    jp = tmp_path / "jp.jsonl"
    _write_jsonl(ca, ca_rows)
    _write_jsonl(jp, jp_rows)
    json_out = tmp_path / "report.json"
    md_out = tmp_path / "report.md"
    rc = compare.main(
        [
            "--baseline-input", str(ca), "--baseline-label", "california",
            "--baseline-artifact", str(tmp_path / "ca.pcsh"),
            "--input", str(jp), "--label", "tokyo",
            "--artifact", str(tmp_path / "jp.pcsh"),
            "--json-out", str(json_out), "--markdown-out", str(md_out),
        ]
    )
    assert rc == 0
    report = json.loads(json_out.read_text())
    assert report["baseline"]["build"]["bytes_per_place"] > 0
    assert report["candidate"]["build"]["bytes_per_place"] > 0
    # The Tokyo fixture has CJK-dominant tokens; California does not.
    assert report["candidate"]["script_mix"]["cjk_dominant_tokens"] > 0
    assert report["baseline"]["script_mix"]["cjk_dominant_tokens"] == 0
    # Shard build stays correct on both partitions.
    assert report["candidate"]["derived_oracle"]["complete_candidate_recall"] is True
    assert report["candidate"]["fixed_case_oracle"]["complete_candidate_recall"] is True
    text = md_out.read_text()
    assert "non-California partition" in text
    assert "tokyo" in text and "california" in text
