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
