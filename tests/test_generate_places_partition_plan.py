from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "generate_places_partition_plan", ROOT / "scripts/generate_places_partition_plan.py"
)
assert SPEC and SPEC.loader
GEN = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = GEN
SPEC.loader.exec_module(GEN)

COMMITTED = ROOT / "scripts/places_partition_plan_v1.json"


def test_generator_caps_match_the_caps_the_hosted_build_enforces():
    # A plan generated against looser caps than the build enforces would place
    # partitions the reducer then rejects; tighter, and the offline generator
    # silently over-subdivides. Neither is caught anywhere else, because the
    # generator runs on a different machine from the build.
    sys.path.insert(0, str(ROOT / "scripts"))
    import construction_v1_hosted as HOSTED

    places = HOSTED.HOSTED_LIMITS["places"]
    assert GEN.DEFAULT_CAPS["term_rows"] == places["partition_term_rows"]
    assert GEN.DEFAULT_CAPS["distinct_tokens"] == places["partition_distinct_tokens"]
    assert (
        GEN.DEFAULT_CAPS["estimated_uncompressed_bytes"]
        == places["partition_estimated_bytes"]
    )


def plan_with(partitions):
    return {"schema": "x", "partitions": partitions}


def partition(cell, depth, prefix, rows=1, size=1, tokens=1):
    return {
        "partition_cell": cell,
        "ownership": {"kind": GEN.SCHEME_KIND, "depth": depth, "prefix": prefix},
        "term_rows": rows,
        "estimated_uncompressed_bytes": size,
        "distinct_tokens": tokens,
    }


def test_prefix_matches_the_sql_subdivision_expression():
    # places_construction_v1._prefix_sql: (token_hash >> (64 - depth * 4))
    token_hash = 0xABCDEF0123456789
    assert GEN.prefix_of(token_hash, 1) == 0xA
    assert GEN.prefix_of(token_hash, 2) == 0xAB
    assert GEN.prefix_of(token_hash, 8) == 0xABCDEF01
    for bad in (0, -1, GEN.MAXIMUM_DEPTH + 1):
        with pytest.raises(ValueError):
            GEN.prefix_of(token_hash, bad)


def test_committed_plan_is_reproducible_from_its_source(tmp_path):
    # The committed plan must be exactly what the generator emits; otherwise the
    # tree in the repo is hand-edited and nothing proves it is derivable.
    committed = json.loads(COMMITTED.read_text())
    leaves = [
        GEN.Leaf(cell, int(branch.split(":")[0]), int(branch.split(":")[1], 16), 0, 0, 0)
        for cell, branches in committed["cells"].items()
        for branch in branches
    ]
    assert GEN.build_tree(leaves) == committed["cells"]


def test_committed_plan_reconstructs_its_recorded_partition_count():
    committed = json.loads(COMMITTED.read_text())
    branches = sum(len(value) for value in committed["cells"].values())
    # Cells in the tree are replaced by their branches; every other populated
    # cell contributes exactly one depth-0 partition.
    subdivided_cells = len(committed["cells"])
    recorded = committed["generated_from"]["partitions"]
    assert branches == 1_388
    assert subdivided_cells == 83
    assert recorded == 17_816
    assert recorded - branches + subdivided_cells == 16_511  # populated cells


def test_headroom_splits_only_leaves_over_the_threshold():
    caps = dict(GEN.DEFAULT_CAPS)
    hot = partition("aaaa", 0, 0, rows=caps["term_rows"] // 2 + 1)
    cold = partition("bbbb", 0, 0, rows=1)
    leaves = GEN.leaves_from_plan_payload(plan_with([hot, cold]))
    result, split = GEN.apply_headroom(leaves, caps, 0.5)
    assert split == 1
    assert len(result) == 17  # 16 children for the hot cell, 1 untouched cold
    assert sorted(leaf.prefix for leaf in result if leaf.cell == "aaaa") == list(range(16))
    assert all(leaf.depth == 1 for leaf in result if leaf.cell == "aaaa")
    assert [leaf.depth for leaf in result if leaf.cell == "bbbb"] == [0]


def test_headroom_none_is_a_pure_passthrough():
    caps = dict(GEN.DEFAULT_CAPS)
    leaves = GEN.leaves_from_plan_payload(
        plan_with([partition("aaaa", 0, 0, rows=caps["term_rows"])])
    )
    result, split = GEN.apply_headroom(leaves, caps, None)
    assert split == 0
    assert result == leaves


def test_headroom_extends_an_already_subdivided_branch():
    caps = dict(GEN.DEFAULT_CAPS)
    leaves = GEN.leaves_from_plan_payload(
        plan_with([partition("aaaa", 1, 0xC, rows=caps["term_rows"])])
    )
    result, _ = GEN.apply_headroom(leaves, caps, 0.5)
    assert {leaf.prefix for leaf in result} == {0xC0 | child for child in range(16)}
    assert all(leaf.depth == 2 for leaf in result)
    tree = GEN.build_tree(result)
    assert tree["aaaa"][0] == "2:c0"


def test_headroom_never_exceeds_the_maximum_depth():
    caps = dict(GEN.DEFAULT_CAPS)
    leaves = GEN.leaves_from_plan_payload(
        plan_with([partition("aaaa", GEN.MAXIMUM_DEPTH, 0, rows=caps["term_rows"])])
    )
    result, split = GEN.apply_headroom(leaves, caps, 0.5)
    assert split == 0
    assert [leaf.depth for leaf in result] == [GEN.MAXIMUM_DEPTH]


def test_build_tree_omits_depth_zero_cells():
    leaves = [GEN.Leaf("aaaa", 0, 0, 1, 1, 1), GEN.Leaf("bbbb", 1, 3, 1, 1, 1)]
    assert GEN.build_tree(leaves) == {"bbbb": ["1:3"]}


def test_rejects_an_unknown_ownership_scheme():
    payload = plan_with([partition("aaaa", 0, 0)])
    payload["partitions"][0]["ownership"]["kind"] = "some-other-scheme-v9"
    with pytest.raises(ValueError, match="ownership kind"):
        GEN.leaves_from_plan_payload(payload)


def test_render_is_one_cell_per_line_and_parses():
    document = {
        "schema": GEN.SCHEMA,
        "cells": {"aaaa": ["1:0", "1:1"], "bbbb": ["2:ff"]},
    }
    payload = GEN.render(document)
    assert json.loads(payload)["cells"] == document["cells"]
    body = payload.decode().splitlines()
    assert sum(1 for line in body if line.startswith('    "')) == 2
