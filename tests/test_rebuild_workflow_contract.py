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
import sys
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
    # Only the catalog-coupled phase (5) can strand a worker on a version it is
    # about to delete, so only its delete must follow the wait. Phases 1-4 never
    # touch catalog.json (asserted separately below) and precede it.
    cleanup = jobs(load(CLEANUP))["cleanup"]
    names = step_names(cleanup)
    wait = names.index("Wait out worker catalog cache TTL")
    assert names.index("Prune catalog and verify") < wait
    assert wait < names.index("Phase 5 - delete pruned version prefixes")
    assert re.search(r"sleep\s+\d+", step_by_name(cleanup, "Wait out worker catalog cache TTL")["run"])


# --- retargeted 2026-07 cleanup: phase gating, protected prefixes, prefix regex


def _cleanup_job():
    return jobs(load(CLEANUP))["cleanup"]


def test_only_phase_five_steps_touch_the_root_catalog():
    """Phases 1-4 are dispatchable alone and must not mutate catalog.json."""
    for step in _cleanup_job()["steps"]:
        run = step.get("run", "")
        writes = re.search(r"cp\s+\S+\s+\"s3://\$BUCKET/catalog\.json\"", run)
        prunes = "prune_catalog.py allowlist" in run
        if writes or prunes:
            gate = step.get("if", "")
            assert "phase5" in gate, step.get("name")


def test_catalog_prune_is_gated_so_prune_argument_is_never_empty():
    prune = step_by_name(_cleanup_job(), "Prune catalog and verify")
    assert prune["if"] == "steps.plan.outputs.phase5 == 'true'"
    assert '--prune "$PRUNE_VERSIONS"' in prune["run"]
    assert load(CLEANUP)["env"]["PRUNE_VERSIONS"].split(), "PRUNE_VERSIONS is empty"


def test_every_phase_is_independently_gated_on_a_dispatch_selection():
    names = [n or "" for n in step_names(_cleanup_job())]
    phase_steps = [n for n in names if n.startswith("Phase ")]
    assert len(phase_steps) == 5, phase_steps
    seen = set()
    for step in _cleanup_job()["steps"]:
        name = step.get("name") or ""
        number = re.match(r"Phase (\d) - ", name)
        if not number:
            continue
        # The gate must name the *same* phase as the step: a mis-numbered gate
        # would run a phase the dispatcher did not select.
        assert step["if"] == (
            f"steps.plan.outputs.phase{number.group(1)} == 'true'"
        ), name
        seen.add(number.group(1))
    assert seen == {"1", "2", "3", "4", "5"}, seen


def test_ttl_sleep_is_gated_on_the_same_phase_five_selection():
    gate = "steps.plan.outputs.phase5 == 'true'"
    for name in (
        "Prune catalog and verify",
        "Verify live worker before deleting",
        "Wait out worker catalog cache TTL",
        "Phase 5 - delete pruned version prefixes",
    ):
        assert step_by_name(_cleanup_job(), name)["if"] == gate, name


def test_protected_prefix_guard_precedes_every_delete():
    steps = _cleanup_job()["steps"]
    names = [step.get("name") for step in steps]
    guard = names.index("Assert no target intersects a protected prefix")
    for index, step in enumerate(steps):
        # Strip the sourced helper's definition: the plan step *writes* the
        # `s3 rm` helper, it does not run one.
        body = re.sub(r"cat > /tmp/r2-rm\.sh <<'SH'.*?\nSH\n", "", step.get("run", ""), flags=re.S)
        if "s3 rm" in body or re.search(r"^\s*RM \"", body, re.M):
            assert guard < index, step.get("name")
    protected = load(CLEANUP)["env"]["PROTECTED_PREFIXES"].split()
    assert protected == ["2026-07-13.0", "2026-07-18.0", "2026-07-02.3", "backups"]
    # No target constant may name a protected prefix.
    env = load(CLEANUP)["env"]
    targets = [
        version
        for key in ("STAGING_ONLY", "ORPHAN_PREFIXES", "PRUNE_VERSIONS")
        for version in env[key].split()
    ]
    assert targets, "no cleanup targets configured"
    assert not set(targets) & set(protected), targets


def test_protected_prefix_guard_rejects_intersecting_targets(tmp_path):
    """Run the guard's own python, as the workflow does, over crafted targets."""
    guard = step_by_name(
        _cleanup_job(), "Assert no target intersects a protected prefix"
    )["run"]
    body = guard.split("<<'PY'\n", 1)[1].rsplit("PY", 1)[0]
    script = tmp_path / "guard.py"
    script.write_text(body)
    protected = "2026-07-13.0 2026-07-18.0 2026-07-02.3 backups"
    # The workflow passes the target file as argv[2]; keep this test's file in
    # tmp_path so it never touches the real /tmp/delete-targets.txt.
    targets = tmp_path / "delete-targets.txt"

    def run(lines):
        targets.write_text("".join(f"{line}\n" for line in lines))
        return subprocess.run(
            [sys.executable, str(script), protected, str(targets)],
            capture_output=True,
            text=True,
        )

    ok = run(["1|2026-07-02.0/staging/", "3|2026-07-17.0/"])
    assert ok.returncode == 0, ok.stdout + ok.stderr

    for bad in (
        "1|2026-07-18.0/staging/",       # inside a protected prefix
        "3|2026-07-13.0/",               # the live latest
        "5|2026-07-02.3/",               # id-index-bearing rollback
        "1|backups/catalog-x.json",      # catalog backups
        "3|staging/",                    # denied pipeline root
        "3|staging/global-v2/",           # denied pipeline root
        "5|catalog.json",                 # the catalog itself
        "5|2026-07-02.1/catalog.json",    # a catalog at any depth
        "3|2026-07-13.0 /",               # whitespace defeating the comparison
        "3| 2026-07-13.0/",               # leading whitespace
        "3|2026-07-13.0\t/",              # tab
        "2|/2026-07-17.0/",              # absolute
        "3|2026-07-17.0/../2026-07-13.0/",  # traversal
        "3|foo",                          # unqualified, no slash
    ):
        result = run([bad])
        assert result.returncode == 1, (bad, result.stdout)
        assert "::error::" in result.stdout, bad


def test_global_v2_inputs_reject_multiline_values():
    """A two-line value would pass the per-line grep and smuggle line 2 in."""
    plan = step_by_name(_cleanup_job(), "Validate inputs and compute deletion targets")
    check = plan["run"].split("check_global_v2() {", 1)[1]
    # The newline/whitespace rejection must come before the grep that only sees
    # the first line.
    reject = check.index("must be a single line")
    grep = check.index("[0-9a-f]{64}")
    assert reject < grep, "multi-line rejection must precede the regex check"
    for pattern in ("*$'\\n'*", "*$'\\r'*", "*$'\\t'*"):
        assert pattern in check, pattern


def test_a_mistyped_confirmation_fails_the_run():
    workflow = load(CLEANUP)
    confirm = jobs(workflow)["confirm"]
    # Ungated job, so a typo is a red run rather than a green skip...
    assert "if" not in confirm
    run = confirm["steps"][0]["run"]
    assert '"$CONFIRM" != "CLEANUP"' in run and "exit 1" in run
    # ...and the cleanup job still carries its own gate and waits on it.
    cleanup = jobs(workflow)["cleanup"]
    assert cleanup["if"] == "github.event.inputs.confirm == 'CLEANUP'"
    assert cleanup["needs"] == "confirm"


def test_dry_run_input_routes_every_delete_through_dryrun():
    workflow = load(CLEANUP)
    triggers = workflow[True] if True in workflow else workflow["on"]
    dry = triggers["workflow_dispatch"]["inputs"]["dry_run"]
    assert dry["type"] == "boolean" and dry["default"] is False
    assert workflow["env"]["DRY_RUN"] == "${{ github.event.inputs.dry_run }}"

    plan = step_by_name(_cleanup_job(), "Validate inputs and compute deletion targets")
    helper = plan["run"].split("cat > /tmp/r2-rm.sh", 1)[1].split("\nSH\n", 1)[0]
    assert '"${DRY_RUN:-false}" = "true"' in helper
    assert "--dryrun" in helper
    # Real deletes keep an audit trail rather than suppressing per-key output.
    assert not re.search(r"s3 rm[^\n]*--only-show-errors", helper)
    assert "/tmp/deleted-keys.log" in helper

    # Every phase deletes through the shared helper; no raw `s3 rm` survives.
    for step in _cleanup_job()["steps"]:
        name = step.get("name") or ""
        if name.startswith("Phase ") or name == "Prune catalog and verify":
            body = step.get("run", "")
            assert "s3 rm" not in body, name
            if name.startswith("Phase "):
                assert "source /tmp/r2-rm.sh" in body, name


def test_dry_run_mode_is_announced_and_the_audit_log_is_uploaded():
    steps = _cleanup_job()["steps"]
    announce = step_by_name(_cleanup_job(), "Announce dry-run mode")
    assert "DRY RUN" in announce["run"]
    assert "GITHUB_STEP_SUMMARY" in announce["run"]
    upload = [s for s in steps if "upload-artifact" in (s.get("uses") or "")]
    assert len(upload) == 1, upload
    assert upload[0]["with"]["path"] == "/tmp/deleted-keys.log"


def test_global_v2_prefix_regex_guard_is_full_digest_only():
    plan = step_by_name(_cleanup_job(), "Validate inputs and compute deletion targets")
    pattern = r"^staging/global-v2/[0-9a-f]{64}/$"
    assert pattern in plan["run"]
    # Both the bucket-root phases re-validate their own target before deleting.
    for name in (
        "Phase 2 - delete orphaned global-v2 staging prefix",
        "Phase 4 - delete global-v2 address map objects only",
    ):
        assert pattern in step_by_name(_cleanup_job(), name)["run"], name

    digest = "5" + "9f326dc2fd0866f54ead2ce0a1b19b5b9955c565cd8ef662d6bf22fc1047a6" + "3"
    accept = f"staging/global-v2/{digest}/"
    assert re.fullmatch(pattern[1:-1], accept)
    for bad in (
        "staging/",
        "staging/global-v2/",
        f"staging/global-v2/{digest[:8]}/",       # truncated digest
        f"staging/global-v2/{digest}",            # no trailing slash
        f"staging/global-v2/{digest.upper()}/",   # not lowercase hex
        f"staging/global-v2/{digest}/immutable/",  # deeper than the digest
        "",
    ):
        assert not re.fullmatch(pattern[1:-1], bad), bad
    # A bare staging value is refused by an explicit case guard too, not just
    # by the regex.
    assert "refuses bare staging prefix" in plan["run"]


def test_phase_four_deletes_only_the_addresses_object_subtree():
    env = load(CLEANUP)["env"]
    assert env["ADDRESSES_SUBTREE"] == "immutable/map/addresses/objects/"
    run = step_by_name(
        _cleanup_job(), "Phase 4 - delete global-v2 address map objects only"
    )["run"]
    assert 'may only delete $ADDRESSES_SUBTREE' in run
    # The retained benchmark evidence is asserted present after the delete.
    assert 'for keep in "immutable/inventory/" "immutable/map/" "completed/"' in run
    assert "evidence lost" in run


def test_cleanup_keeps_inventory_and_live_probe_bookends():
    names = [n or "" for n in step_names(_cleanup_job())]
    phases = [index for index, name in enumerate(names) if name.startswith("Phase ")]
    pre = names.index("Pre-run live worker probe and inventory")
    post = names.index("Post-run live worker probe and inventory")
    assert pre < min(phases)
    # The post-run bookend follows every phase; only the audit-trail reporting
    # steps come after it.
    assert post > max(phases)
    assert names[post + 1:] == [
        "Summarize the delete audit trail",
        "Upload the delete audit log",
    ]
    for name in (
        "Pre-run live worker probe and inventory",
        "Post-run live worker probe and inventory",
    ):
        run = step_by_name(_cleanup_job(), name)["run"]
        for path in ("/health", "/search?q=", "/reverse?lat=", "/id/"):
            assert path in run, (name, path)
        assert "Total Objects" in run, name


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
