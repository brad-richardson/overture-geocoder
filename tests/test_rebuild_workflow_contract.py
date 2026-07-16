"""Structural contract tests for the R2 rebuild/patch/cleanup workflows.

These parse the workflow YAML and assert *structural* invariants (needs-graph,
concurrency, matrices, step ordering, which jobs may write the root catalog)
rather than matching raw workflow text, so they do not go vacuous when step
names or wording drift.

Note: PyYAML parses the ``on:`` trigger key as the boolean ``True`` (YAML 1.1),
so a workflow's triggers live under ``wf[True]`` — none of the checks below need
them, but keep it in mind before adding trigger assertions.
"""

import json
import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
REBUILD = WORKFLOWS / "rebuild-r2-shards.yml"
CLEANUP = WORKFLOWS / "r2-cleanup.yml"
PATCH = WORKFLOWS / "patch-id-stage.yml"
STAC_SOURCES = sorted(
    (ROOT / "crates" / "geocoder-worker" / "src").glob("stac*/**/*.rs")
) + [ROOT / "crates" / "geocoder-worker" / "src" / "stac.rs"]

# The 3-hex-prefix build fan-out is split into these four ranges everywhere the
# ID pipeline references them (rebuild staging + build, patch build + marker
# invalidation). All definitions must agree with this canonical set.
CANONICAL_RANGES = {("000", "3ff"), ("400", "7ff"), ("800", "bff"), ("c00", "fff")}


def load(path):
    return yaml.safe_load(path.read_text())


def jobs(wf):
    return wf["jobs"]


def step_by_name(job, name):
    for step in job.get("steps", []):
        if step.get("name") == name:
            return step
    raise AssertionError(f"step {name!r} not found; have {step_names(job)}")


def step_names(job):
    return [s.get("name") for s in job.get("steps", [])]


def _join_continuations(script):
    # Collapse `\<newline>  ` shell line-continuations so a command and its
    # arguments live on one logical line for matching.
    return re.sub(r"\\\n\s*", " ", script)


# The root discovery catalog is written two ways now, both owned by the
# finalize jobs:
#   1. `aws s3 cp <source> "s3://geocoder-shards/catalog.json"` — the rebuild
#      retention prune's publish (and the cleanup workflow's).
#   2. the tested Python `finalize_rebuild.py promote|recover` paths, which own
#      the compare-before-swap publish, rollback, and crash-window restore.
# Reads go through scripts/r2_catalog_fetch.sh and never name the literal, and
# backups target `.../backups/catalog-*.json`, so these patterns match
# root-catalog writers exclusively.
CATALOG_WRITE = re.compile(r'aws s3 cp\s+\S+\s+"s3://geocoder-shards/catalog\.json"')
CATALOG_WRITE_PY = re.compile(r"finalize_rebuild\.py\s+(?:promote|recover)\b")


def _writes_root_catalog(job):
    for step in job.get("steps", []):
        run = step.get("run")
        if not run:
            continue
        joined = _join_continuations(run)
        if CATALOG_WRITE.search(joined) or CATALOG_WRITE_PY.search(joined):
            return True
    return False


def _ranges_from_include(job):
    include = job["strategy"]["matrix"]["include"]
    return {(str(e["prefix_start"]), str(e["prefix_end"])) for e in include}


def _ranges_from_extra_args(job):
    ranges = set()
    for entry in job["strategy"]["matrix"]["include"]:
        m = re.search(r"--prefix-start\s+(\w+)\s+--prefix-end\s+(\w+)", entry["extra_args"])
        assert m, f"no prefix range in {entry!r}"
        ranges.add((m.group(1), m.group(2)))
    return ranges


def _ranges_from_for_loop(script):
    m = re.search(r"for RANGE in ([0-9a-f\- ]+?)\s*;?\s*(?:\n|do)", script)
    assert m, "no `for RANGE in ...` loop found"
    return {tuple(tok.split("-")) for tok in m.group(1).split()}


def _stac_const(name):
    text = "\n".join(p.read_text() for p in STAC_SOURCES if p.exists())
    # Rust numeric literals may carry underscore separators (e.g. 60_000).
    m = re.search(rf"{name}[^=]*=\s*(\d[\d_]*)", text)
    assert m, f"{name} not found in the worker stac module"
    return int(m.group(1).replace("_", ""))


# --- (a) only the finalize jobs may write the root catalog -------------------


def test_only_finalize_jobs_write_the_root_catalog():
    writers = {name for name, job in jobs(load(REBUILD)).items() if _writes_root_catalog(job)}
    assert writers == {"finalize-release", "post-finalize"}, writers


# --- (b) finalize-release needs-graph and completeness gate ------------------


def test_finalize_release_needs_graph_and_completeness_gate():
    finalize = jobs(load(REBUILD))["finalize-release"]
    assert finalize["needs"] == ["prep", "rebuild-shards", "id-post"]
    gate = " ".join(finalize["if"].split())
    assert "needs.rebuild-shards.result == 'success'" in gate
    assert "needs.id-post.result == 'success'" in gate
    assert "needs.prep.outputs.complete == 'true'" in gate


# --- (c) one shared production-catalog concurrency group ----------------------


def test_all_catalog_writers_share_one_concurrency_group():
    for path in (REBUILD, CLEANUP, PATCH):
        assert load(path)["concurrency"]["group"] == "r2-production-catalog", path.name


# --- (d) hex-range matrices are identical everywhere -------------------------


def test_hex_range_matrices_are_identical_everywhere():
    rebuild = jobs(load(REBUILD))
    patch = jobs(load(PATCH))
    sources = {
        "rebuild id-build": _ranges_from_include(rebuild["id-build"]),
        "rebuild id-stage-registry": _ranges_from_extra_args(rebuild["id-stage-registry"]),
        "patch build-ranges": _ranges_from_include(patch["build-ranges"]),
        "patch force-rebuild loop": _ranges_from_for_loop(
            step_by_name(
                patch["patch-stage"], "Force complete rebuilds of affected ranges"
            )["run"]
        ),
    }
    for label, ranges in sources.items():
        assert ranges == CANONICAL_RANGES, f"{label}: {ranges}"


# --- (e) deletes are preceded by the cache-TTL wait --------------------------


def test_cleanup_waits_out_cache_ttl_before_deleting():
    cleanup = jobs(load(CLEANUP))["cleanup"]
    names = step_names(cleanup)
    wait = names.index("Wait out worker catalog cache TTL")
    for delete_step in ("Delete pruned and orphaned prefixes", "Phase 2 - wipe latest staging"):
        assert wait < names.index(delete_step), delete_step
    assert re.search(r"sleep\s+\d+", step_by_name(cleanup, "Wait out worker catalog cache TTL")["run"])


def test_rebuild_prune_waits_between_catalog_publish_and_delete():
    prune = step_by_name(
        jobs(load(REBUILD))["post-finalize"],
        "Prune only after successful promotion and smoke",
    )["run"]
    publish = prune.index('"s3://geocoder-shards/catalog.json"')
    wait = re.search(r"sleep\s+\d+", prune)
    delete = prune.index("aws s3 rm")
    assert wait is not None, "no sleep in prune step"
    assert publish < wait.start() < delete


# --- item-1 coupling: retention floor + TTL wait track worker constants ------


def test_retention_and_ttl_track_worker_constants():
    fallback = _stac_const("MAX_VERSION_ATTEMPTS")  # 4
    cache_ttl = _stac_const("CATALOG_CACHE_TTL")  # 300
    # Worst-case catalog staleness stacks the edge-cache TTL with the
    # in-isolate text memo: a memo entry written at the last instant of the
    # cache TTL can serve the pre-prune catalog for another TEXT_MEMO_TTL_MS.
    text_memo_s = _stac_const("TEXT_MEMO_TTL_MS") // 1000  # 60
    worst_case_staleness = cache_ttl + text_memo_s  # 360

    prune = step_by_name(
        jobs(load(REBUILD))["post-finalize"],
        "Prune only after successful promotion and smoke",
    )["run"]
    keep = re.search(r"KEEP_VERSIONS=(\d+)", prune)
    assert keep and int(keep.group(1)) == fallback

    cleanup_prune = step_by_name(
        jobs(load(CLEANUP))["cleanup"], "Prune catalog and verify"
    )["run"]
    # The floor now lives in the shared prune_catalog.py and is passed as the
    # --floor argument, which must still track the worker fallback depth.
    floor = re.search(r"--floor\s+(\d+)", cleanup_prune)
    assert floor and int(floor.group(1)) == fallback

    for path, job_name, step_name in (
        (REBUILD, "post-finalize", "Prune only after successful promotion and smoke"),
        (CLEANUP, "cleanup", "Wait out worker catalog cache TTL"),
    ):
        run = step_by_name(jobs(load(path))[job_name], step_name)["run"]
        sleeps = [int(s) for s in re.findall(r"sleep\s+(\d+)", run)]
        assert sleeps and max(sleeps) >= worst_case_staleness, (
            path.name, step_name, sleeps,
        )


# --- behavioral: the jq guard patch-id-stage uses to reject a live version ---


def test_patch_rejects_catalogued_version_via_jq_guard():
    reject = step_by_name(
        jobs(load(PATCH))["patch-stage"], "Reject immutable finalized releases"
    )["run"]
    assert "any(.links[]?;" in reject  # the guard the jq below exercises

    catalog = {
        "links": [
            {"rel": "child", "href": "./2026-07-13.0/collection.json"},
            {"rel": "child", "href": "./2026-06-01.0/collection.json"},
        ]
    }
    result = subprocess.run(
        [
            "jq", "-e", "--arg", "version", "2026-07-13.0",
            'any(.links[]?; .rel == "child" and '
            '((.href | ltrimstr("./") | split("/")[0]) == $version))',
        ],
        input=json.dumps(catalog),
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0
