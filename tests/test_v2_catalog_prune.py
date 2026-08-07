"""Tests for `v2_release_manifest.py prune`.

The v2 catalog had no retention path at all: releases accumulated until they hit
the Worker's MAX_CATALOG_RELEASES ceiling of 64, at which point the Worker
REJECTS the catalog outright. `v2_retention_guard.py` could already answer "is
this prefix referenced"; nothing could make a prefix stop being referenced.
"""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
SPEC = importlib.util.spec_from_file_location(
    "v2_release_manifest", ROOT / "scripts" / "v2_release_manifest.py"
)
V2 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(V2)


def entry(build, overture="2026-06-17.0"):
    return {
        "geocoder_build": build,
        "overture_release": overture,
        "manifest_key": f"v2/releases/{build}/release.json",
        "manifest_sha256": "a" * 64,
        "release_digest": "b" * 64,
    }


def catalog(builds):
    body = {
        "schema": V2.CATALOG_SCHEMA,
        "generated_at": "2026-08-07T00:00:00+00:00",
        "latest": builds[0],
        "releases": [entry(b) for b in builds],
    }
    body["catalog_digest"] = V2.gbm.digest(body)
    return body


BUILDS = ["2026-08-07.0", "2026-08-03.0", "2026-08-02.0", "2026-07-31.0", "2026-07-28.0"]


# --- keep mode ---------------------------------------------------------------


def test_keep_retains_the_newest_n_and_reports_the_rest():
    pruned, dropped = V2.build_pruned_catalog(
        catalog(BUILDS), keep=2, generated_at="2026-08-07T01:00:00+00:00"
    )
    assert [e["geocoder_build"] for e in pruned["releases"]] == BUILDS[:2]
    assert dropped == BUILDS[2:]
    assert pruned["latest"] == "2026-08-07.0"


def test_pruned_catalog_revalidates_and_reseals_its_digest():
    pruned, _ = V2.build_pruned_catalog(
        catalog(BUILDS), keep=3, generated_at="2026-08-07T01:00:00+00:00"
    )
    # The digest must cover the NEW release list, not the old one.
    unsigned = {k: v for k, v in pruned.items() if k != "catalog_digest"}
    assert V2.gbm.digest(unsigned) == pruned["catalog_digest"]
    V2.validate_catalog(pruned)


def test_keep_at_or_above_current_depth_is_a_no_op():
    before = catalog(BUILDS)
    pruned, dropped = V2.build_pruned_catalog(before, keep=len(BUILDS))
    assert dropped == []
    assert pruned == before


def test_keep_below_one_is_refused():
    for bad in (0, -1):
        with pytest.raises(ValueError, match="keep must be >= 1"):
            V2.build_pruned_catalog(catalog(BUILDS), keep=bad)


def test_keep_one_is_allowed_because_v2_has_no_fallback_chain():
    # Unlike the v1 root, which the Worker walks up to MAX_VERSION_ATTEMPTS
    # deep, load_available_release loads the single current release and 503s.
    # Depth is operator rollback convenience, not a technical floor.
    pruned, dropped = V2.build_pruned_catalog(catalog(BUILDS), keep=1)
    assert [e["geocoder_build"] for e in pruned["releases"]] == ["2026-08-07.0"]
    assert dropped == BUILDS[1:]


# --- retain-builds mode ------------------------------------------------------


def test_retain_builds_keeps_exactly_the_named_set_in_catalog_order():
    # The cost-aware case: retain the latest plus a CHEAPER older rollback
    # target than the immediately-preceding one.
    pruned, dropped = V2.build_pruned_catalog(
        catalog(BUILDS), retain_builds=["2026-08-02.0", "2026-08-07.0"]
    )
    assert [e["geocoder_build"] for e in pruned["releases"]] == [
        "2026-08-07.0",
        "2026-08-02.0",
    ]
    assert dropped == ["2026-08-03.0", "2026-07-31.0", "2026-07-28.0"]


def test_retain_builds_rejects_a_build_absent_from_the_catalog():
    with pytest.raises(ValueError, match="absent from the catalog"):
        V2.build_pruned_catalog(
            catalog(BUILDS), retain_builds=["2026-08-07.0", "2099-01-01.0"]
        )


def test_retain_builds_deduplicates():
    pruned, _ = V2.build_pruned_catalog(
        catalog(BUILDS), retain_builds=["2026-08-07.0", "2026-08-07.0"]
    )
    assert len(pruned["releases"]) == 1


# --- invariants that hold in both modes --------------------------------------


def test_dropping_latest_is_refused():
    with pytest.raises(ValueError, match="refusing to drop the catalog latest"):
        V2.build_pruned_catalog(catalog(BUILDS), retain_builds=["2026-08-03.0"])


def test_exactly_one_mode_is_required():
    for kwargs in ({}, {"keep": 2, "retain_builds": ["2026-08-07.0"]}):
        with pytest.raises(ValueError, match="exactly one of keep or retain_builds"):
            V2.build_pruned_catalog(catalog(BUILDS), **kwargs)


def test_an_invalid_catalog_is_refused_before_anything_is_dropped():
    broken = catalog(BUILDS)
    broken["catalog_digest"] = "c" * 64
    with pytest.raises(ValueError):
        V2.build_pruned_catalog(broken, keep=2)


def test_prune_never_reorders_or_rewrites_retained_entries():
    before = catalog(BUILDS)
    pruned, _ = V2.build_pruned_catalog(before, keep=3)
    assert pruned["releases"] == before["releases"][:3]


# --- against the real live catalog -------------------------------------------

LIVE = Path("/tmp/claude-1000/-home-brad-dev-overture-geocoder")


def test_matches_the_live_catalog_shape_if_a_copy_is_available():
    # Opportunistic: if a real catalog copy is lying around from an audit, prove
    # the pruner accepts production bytes rather than only the fixture shape.
    candidates = list(LIVE.rglob("v2-catalog.json")) if LIVE.exists() else []
    if not candidates:
        pytest.skip("no live catalog copy available")
    live = json.loads(candidates[0].read_text())
    pruned, dropped = V2.build_pruned_catalog(live, keep=2)
    assert pruned["latest"] == live["latest"]
    assert len(pruned["releases"]) == 2
    assert len(dropped) == len(live["releases"]) - 2
    V2.validate_catalog(pruned)
