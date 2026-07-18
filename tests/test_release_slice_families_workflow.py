"""Structural contract tests for the family-release-slice workflow.

These assert the safety and integration invariants of
``.github/workflows/release-slice-families.yml``:

* it is workflow_dispatch-only and main-only;
* the slice version is guarded against the production release pattern;
* NOTHING writes the root catalog, and a negative probe proves the production
  catalog never references the slice;
* the ONE writer of the slice prefix is ``finalize_rebuild.py publish-family`` --
  no raw ``aws`` upload and no ``r2_verified_store`` bypasses the finalizer;
* cleanup is opt-in, always-run, and prefix-guarded.

They parse the YAML and match command text rather than pinning step wording, so
they do not go vacuous when steps are renamed.

Note: PyYAML parses the ``on:`` trigger key as the boolean ``True`` (YAML 1.1).
"""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "release-slice-families.yml"


def load():
    return yaml.safe_load(WORKFLOW.read_text())


def jobs():
    return load()["jobs"]


def _join_continuations(script):
    return re.sub(r"\\\n\s*", " ", script)


def all_run_scripts():
    scripts = []
    for job in jobs().values():
        for step in job.get("steps", []):
            run = step.get("run")
            if run:
                scripts.append(_join_continuations(run))
    return scripts


def test_workflow_is_dispatch_only():
    triggers = load()[True]
    assert set(triggers) == {"workflow_dispatch"}


def test_slice_version_input_is_required_with_no_default():
    slice_input = load()[True]["workflow_dispatch"]["inputs"]["slice_version"]
    assert slice_input["required"] is True
    # A default could smuggle a production-shaped string; it must be operator-set.
    assert "default" not in slice_input


def test_dispatch_input_count_is_within_the_github_limit():
    # workflow_dispatch allows AT MOST 10 inputs (a GitHub hard limit). The four
    # bbox floats per region were collapsed into ONE comma-separated string each
    # (region_a_bbox, region_b_bbox), shared by both families, to stay under it.
    inputs = load()[True]["workflow_dispatch"]["inputs"]
    assert len(inputs) <= 10, sorted(inputs)


def test_regions_are_supplied_as_one_bbox_string_each():
    # Each region is a single "xmin,ymin,xmax,ymax" string, not four scalar
    # inputs; the discrete corner inputs must be gone entirely.
    inputs = load()[True]["workflow_dispatch"]["inputs"]
    assert "region_a_bbox" in inputs
    assert "region_b_bbox" in inputs
    for region in ("a", "b"):
        for corner in ("xmin", "ymin", "xmax", "ymax"):
            assert f"region_{region}_{corner}" not in inputs
        # A default bbox is exactly four comma-separated finite floats.
        parts = str(inputs[f"region_{region}_bbox"]["default"]).split(",")
        assert len(parts) == 4
        for part in parts:
            float(part)


def test_preflight_parses_and_validates_the_bbox_strings():
    # A malformed bbox must fail preflight loudly, before any credentialed job.
    # The parse step routes inputs.*_bbox through env (never inline into the run
    # body) and enforces 4 finite floats with xmin<xmax and ymin<ymax.
    preflight = jobs()["preflight"]
    parse = next(
        step
        for step in preflight["steps"]
        if "parse_bbox" in step.get("run", "")
    )
    env = parse.get("env", {})
    assert env.get("REGION_A_BBOX") == "${{ inputs.region_a_bbox }}"
    assert env.get("REGION_B_BBOX") == "${{ inputs.region_b_bbox }}"
    run = parse["run"]
    # Exactly four comma-separated fields (awk FS=",", NF != 4 fails closed).
    assert 'awk -F' in run and "NF != 4" in run
    # Ordering guards for both axes and a fail-closed exit.
    assert "c[1] + 0 < c[3] + 0" in run
    assert "c[2] + 0 < c[4] + 0" in run
    assert "exit 1" in run
    # inputs.* bbox strings must NOT be interpolated directly into any run body.
    for script in all_run_scripts():
        assert "inputs.region_a_bbox" not in script
        assert "inputs.region_b_bbox" not in script


def test_parsed_corners_route_to_family_jobs_via_preflight_outputs():
    # The validated corners flow to the family builds through preflight job
    # outputs and per-job env -- never re-read from inputs.* by the build jobs.
    workflow = load()
    outs = workflow["jobs"]["preflight"]["outputs"]
    for region in ("a", "b"):
        for corner in ("xmin", "ymin", "xmax", "ymax"):
            key = f"region_{region}_{corner}"
            assert key in outs
            assert "steps.bbox.outputs." + key in outs[key]
    for job_name in ("places", "addresses"):
        env = workflow["jobs"][job_name]["env"]
        for region in ("A", "B"):
            for corner in ("XMIN", "YMIN", "XMAX", "YMAX"):
                val = env[f"REGION_{region}_{corner}"]
                assert "needs.preflight.outputs." in val
                # The build jobs never re-derive corners from raw inputs.
                assert "inputs.region_" not in val


def test_every_job_is_main_only():
    for name, job in jobs().items():
        assert "github.ref == 'refs/heads/main'" in job.get("if", ""), name


def test_dedicated_concurrency_group_never_cancels():
    concurrency = load()["concurrency"]
    assert concurrency["group"] == "release-slice-families"
    # Not the production-catalog group: a slice writes no catalog.
    assert concurrency["group"] != "r2-production-catalog"
    assert concurrency["cancel-in-progress"] is False


def test_no_job_writes_the_root_catalog():
    # Neither the raw `aws s3 cp <x> .../catalog.json` publish nor the tested
    # promote/recover python paths may appear anywhere in the slice workflow.
    catalog_cp = re.compile(r'aws s3 cp\s+\S+\s+"?s3://\S*catalog\.json')
    catalog_py = re.compile(r"finalize_rebuild\.py\s+(?:promote|recover)\b")
    for script in all_run_scripts():
        assert not catalog_cp.search(script), script
        assert not catalog_py.search(script), script


def test_publication_only_through_the_finalizer():
    scripts = all_run_scripts()
    joined = "\n".join(scripts)
    # publish-family is the ONE publication path, exercised for BOTH families.
    publish = [s for s in scripts if "finalize_rebuild.py publish-family" in s]
    families = {
        family
        for script in publish
        for family in re.findall(r"--family\s+(addresses|places)", script)
    }
    assert families == {"addresses", "places"}

    # No raw upload path may write R2: the slice never uses r2_verified_store,
    # never `aws s3api put-object`, never `aws s3 sync ... s3://`, and every
    # `aws s3 cp` reads FROM s3 (source) rather than uploading TO it.
    assert "r2_verified_store" not in joined
    assert "put-object" not in joined
    assert not re.search(r"aws s3 sync\s+\S+\s+\"?s3://", joined)
    for match in re.finditer(r"aws s3 cp\s+(\S+)\s+(\S+)", joined):
        destination = match.group(2).strip('"')
        assert not destination.startswith("s3://"), match.group(0)


def test_slice_version_is_guarded_against_the_production_pattern():
    preflight = jobs()["preflight"]
    guard = "\n".join(
        step.get("run", "") for step in preflight["steps"]
    )
    # Must require the slice shape AND reject the production release shape.
    assert r"slice-[0-9]{4}-[0-9]{2}-[0-9]{2}\.[0-9]+" in guard
    assert r"^[0-9]{4}-[0-9]{2}-[0-9]{2}\.[0-9]+$" in guard


def test_negative_catalog_probe_runs_before_and_after():
    scripts_by_job = {
        name: "\n".join(step.get("run", "") for step in job.get("steps", []))
        for name, job in jobs().items()
    }
    # Probed before any publish (preflight) and after publish (verify).
    assert "probe_catalog_excludes_slice.sh" in scripts_by_job["preflight"]
    assert "probe_catalog_excludes_slice.sh" in scripts_by_job["verify"]


def test_verify_uses_families_only_mode_over_both_families():
    verify = "\n".join(
        step.get("run", "") for step in jobs()["verify"]["steps"]
    )
    assert "finalize_rebuild.py verify-families-only" in verify
    assert "--families addresses,places" in verify


def test_cleanup_is_optin_alwaysrun_and_prefix_guarded():
    cleanup = jobs()["cleanup"]
    condition = " ".join(cleanup["if"].split())
    assert "always()" in condition
    assert "inputs.cleanup" in condition
    run = "\n".join(step.get("run", "") for step in cleanup["steps"])
    # Deletes ONLY the validated slice prefix, and re-guards the pattern first.
    assert r"slice-[0-9]{4}-[0-9]{2}-[0-9]{2}\.[0-9]+" in run
    assert re.search(r'aws s3 rm\s+"s3://geocoder-shards/\$\{SLICE_VERSION\}/"', run)


def test_verify_depends_on_both_family_builds():
    assert set(jobs()["verify"]["needs"]) == {"places", "addresses"}


def test_preflight_rejects_non_nested_region_boxes():
    # The single per-family manifest carries the UNION bbox of both regions, which
    # is an honest coverage claim ONLY when one region box contains the other.
    # Preflight must fail closed on a non-nested pair before any credentialed build
    # rather than publish a manifest that claims coverage over an unbuilt gap.
    preflight = jobs()["preflight"]
    guard = next(
        step
        for step in preflight["steps"]
        if "nested" in step.get("run", "") and "a_in_b" in step.get("run", "")
    )
    run = guard["run"]
    # Both containment directions are tested and the step exits non-zero otherwise.
    assert "a_in_b" in run and "b_in_a" in run
    assert "exit 1" in run
    # The region bounds reach the guard through env (never inline interpolation).
    env = guard.get("env", {})
    for corner in ("XMIN", "YMIN", "XMAX", "YMAX"):
        assert f"REGION_A_{corner}" in env and f"REGION_B_{corner}" in env


def test_address_reconcile_reads_an_isolated_task_rows_dir():
    # region_address_rehearsal.py reconcile globs every *.json in its
    # --task-rows-dir and fails closed on any non-task-rows schema, so the address
    # job must point it at a directory holding ONLY task-rows files, not the build
    # root that also carries the inventory/matrix/summary/map/reduce reports.
    addresses = "\n".join(
        _join_continuations(step.get("run", ""))
        for step in jobs()["addresses"]["steps"]
    )
    reconcile = next(
        line for line in addresses.splitlines() if "reconcile" in line and "--task-rows-dir" in line
    )
    match = re.search(r"--task-rows-dir\s+(\S+)", reconcile)
    assert match, reconcile
    target = match.group(1).strip('"')
    # An isolated subdirectory, not the bare build root.
    assert target.rstrip("/").endswith("/rows"), target


def test_scale_report_is_retained_evidence():
    scale = jobs()["scale-report"]
    assert set(scale["needs"]) == {"places", "addresses", "verify"}
    upload = next(
        step for step in scale["steps"]
        if step.get("uses", "").startswith("actions/upload-artifact")
    )
    assert "slice_scale_report.py" in "\n".join(
        step.get("run", "") for step in scale["steps"]
    )
    # Evidence is retained regardless of downstream failure.
    assert upload["if"] == "${{ always() }}" or "always()" in str(upload.get("if"))
