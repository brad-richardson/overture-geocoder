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

    report = smoke.prepare(
        inputs, tmp_path / "output", head_minimum_candidates=2, head_famous_cap=2
    )

    assert len(report["shards"]) == 3
    assert (tmp_path / "output" / "head.phrp").is_file()
    assert (tmp_path / "output" / "catalog.pcat").is_file()
    assert not (tmp_path / "output" / "head-baseline-nofamous.phrp").exists()
    assert report["catalog"]["schema_version"] == 1
    assert any(case["head_hit"] for case in report["cases"])
    assert any(not case["head_hit"] for case in report["cases"])
    assert any(case["name"] == "shard_prefix" for case in report["cases"])
    assert any(case["route"] == "catalog_point" for case in report["cases"])
    early_exit_names = {
        "shard_no_lexicon_match",
        "shard_empty_intersection",
        "shard_early_exit_sentinel",
    }
    assert all(
        case["result_ids"]
        for case in report["cases"]
        if case["name"] not in early_exit_names
    )
    assert all(
        len(case["required_objects"]) <= 2 for case in report["cases"]
    )
    head = report["head"]
    assert head["head_famous_cap"] == 2
    assert head["famous_delta"]["object_bytes"] == (
        head["object_bytes"] - head["baseline_without_famous"]["object_bytes"]
    )
    assert head["famous_delta"]["key_index_bytes"] >= 0
    assert head["famous_delta"]["key_count"] >= 0
    assert "e2:" in head["eligibility"]


def test_early_exit_cases_pin_the_clause_candidate_counts_contract(tmp_path):
    """The three diagnostics cases carry the agreed counts semantics: numbers
    for clauses whose postings were decoded, null (None) for clauses never read
    because of the no-lexicon-match or emptied-intersection early exits."""
    inputs = [tmp_path / f"input-{index}.json" for index in range(3)]
    for index, path in enumerate(inputs):
        write_places(path, f"R{index}", cjk=index == 2)

    report = smoke.prepare(
        inputs, tmp_path / "output", head_minimum_candidates=2, head_famous_cap=2
    )
    by_name = {case["name"]: case for case in report["cases"]}

    no_match = by_name["shard_no_lexicon_match"]
    assert no_match["clause_candidate_counts"] == [None, None]
    assert no_match["candidate_count"] == 0
    assert no_match["result_ids"] == []

    empty = by_name["shard_empty_intersection"]
    assert len(empty["clause_candidate_counts"]) == 2
    assert all(
        isinstance(count, int) and count >= 1
        for count in empty["clause_candidate_counts"]
    )
    assert empty["candidate_count"] == 0
    assert empty["result_ids"] == []

    sentinel = by_name["shard_early_exit_sentinel"]
    assert len(sentinel["clause_candidate_counts"]) == 3
    assert sentinel["clause_candidate_counts"][:2] == empty["clause_candidate_counts"]
    assert sentinel["clause_candidate_counts"][2] is None
    assert sentinel["candidate_count"] == 0
    assert sentinel["result_ids"] == []

    # Every non-early-exit case decodes all its clauses: no sentinel leaks into
    # ordinary cases, and counts keep one entry per clause.
    for case in report["cases"]:
        assert len(case.get("clause_candidate_counts", case["clauses"])) == len(
            case["clauses"]
        )
        if case["name"] not in (
            "shard_no_lexicon_match",
            "shard_empty_intersection",
            "shard_early_exit_sentinel",
        ) and "clause_candidate_counts" in case:
            assert all(
                isinstance(count, int)
                for count in case["clause_candidate_counts"]
            )


def test_equal_rank_routed_limit_uses_same_doc_tiebreak_as_packed_head(tmp_path):
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

    result = smoke.query_shard(
        artifacts[0], (smoke.Clause("tie"),), [0.0, 0.0]
    )
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

    assert result["candidate_count"] == 11
    assert result["result_ids"] == [f"z{index:02}" for index in range(10)]
    assert head_ids == result["result_ids"]


def famous_head_reader(tmp_path):
    """A packed head where the famous target misses the dense per-token top-10."""
    rows = [
        {
            # "cafe" stays a category-only token so the pair-miss fallback
            # test below has two admitted tokens without an e2: pair key.
            "id": f"dense-{index:02}",
            "name": "Tokyo Grand",
            "category": "cafe",
            "lat": 35.68,
            "lon": 139.75,
            "confidence": 0.95 - index / 1000,
        }
        for index in range(11)
    ]
    rows.append(
        {
            "id": "famous-tower",
            "name": "Tokyo Tower",
            "category": "landmark",
            "lat": 35.6586,
            "lon": 139.7454,
            "confidence": 0.93,
        }
    )
    compact = sys.modules["experiment_places_compact_index"]
    places = [
        compact.place_from_row(row, number) for number, row in enumerate(rows, 1)
    ]
    ordered, heads, _ = smoke.build_heads_and_baseline(
        places,
        head_minimum_candidates=2,
        head_famous_cap=len(places),
        preserve_input_order=True,
    )
    head_path = tmp_path / "head.phrp"
    smoke.build_repack_object(ordered, heads, head_path, head_famous_cap=len(places))
    return smoke.RepackHead(head_path), heads


def test_query_head_serves_famous_pair_before_per_token_intersection(tmp_path):
    reader, heads = famous_head_reader(tmp_path)
    # Without the pair probe this is a zero-result intersection: the famous
    # place is not in the dense token's per-token top-10.
    dense_top = {row for row in heads["e:tokyo"]}
    assert heads["e2:tokyo tower"][0] not in dense_top
    result = smoke.query_head(
        reader, (smoke.Clause("tokyo"), smoke.Clause("tower"))
    )
    assert result["head_hit"] is True
    assert result["result_ids"] == ["famous-tower"]
    # Repeatable, order-stable.
    assert (
        smoke.query_head(reader, (smoke.Clause("tokyo"), smoke.Clause("tower")))[
            "result_ids"
        ]
        == result["result_ids"]
    )
    # Clause order does not change the probed pair key.
    assert (
        smoke.query_head(reader, (smoke.Clause("tower"), smoke.Clause("tokyo")))[
            "result_ids"
        ]
        == result["result_ids"]
    )


def test_query_head_pair_miss_falls_back_to_per_token_intersection(tmp_path):
    reader, heads = famous_head_reader(tmp_path)
    # "cafe" is a category token: never a famous name/brand token, so no
    # e2: pair exists and the query uses the per-token intersection.
    assert "e2:cafe tokyo" not in reader.load_resident_index()
    result = smoke.query_head(
        reader, (smoke.Clause("tokyo"), smoke.Clause("cafe"))
    )
    assert result["head_hit"] is True
    assert result["result_ids"] == [f"dense-{index:02}" for index in range(10)]
    missing = smoke.query_head(
        reader, (smoke.Clause("tokyo"), smoke.Clause("zzabsenttoken"))
    )
    assert missing == {
        "head_hit": False,
        "candidate_count": 0,
        "result_ids": [],
        "results": [],
    }
