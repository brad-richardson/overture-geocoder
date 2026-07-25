"""Contract test pinning the construction-v1 slice smoke workflow.

This job exists because the construction-v1 dry-run mode certifies planning
arithmetic and nothing about the execute data plane (follow-ups item 5). Its
value is entirely in *what it actually runs*, so the properties that make it
real are pinned here: no credentials, the real five phases, the pinned release
and task index, and no write to any production surface.
"""

import importlib.util
import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "slice-smoke.yml"


def text() -> str:
    return WORKFLOW.read_text()


def parsed() -> dict:
    doc = yaml.safe_load(text())
    return doc


def triggers() -> dict:
    doc = parsed()
    return doc[True] if True in doc else doc["on"]


def load_checker():
    path = ROOT / "scripts" / "check_hosted_imports.py"
    spec = importlib.util.spec_from_file_location("check_hosted_imports", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_hosted_imports"] = module
    spec.loader.exec_module(module)
    return module


def test_runs_on_pull_request_and_main_push_path_filtered():
    on = triggers()
    assert set(on) == {"push", "pull_request", "workflow_dispatch"}
    assert on["push"]["branches"] == ["main"]
    for event in ("push", "pull_request"):
        paths = on[event]["paths"]
        assert "scripts/**" in paths
        assert "crates/geocoder-construction/**" in paths
        assert ".github/requirements-hosted-rowgroup.txt" in paths
        assert ".github/workflows/slice-smoke.yml" in paths


def test_needs_no_credentials_and_writes_no_production_surface():
    value = text()
    assert "secrets." not in value
    assert "id-token: write" not in value
    assert "contents: read" in value
    assert "aws s3" not in value.lower()
    assert "wrangler" not in value.lower()
    # finalize publishes to a filesystem remote inside the workspace only.
    assert "R2_ACCESS_KEY_ID" not in value


def test_runs_the_real_five_phase_data_plane_not_a_dry_run():
    value = text()
    assert "scripts/build_slice_inventory_v1.py" in value
    assert "scripts/run_slice_construction_v1.py" in value
    # The harness is what runs the phases; the dry-run-only surfaces must not be
    # what this job exercises.
    assert "--validate-only" not in value
    assert "predict-reduce" not in value
    # The transform/encoder binaries are built the same way the hosted jobs do.
    assert (
        "cargo build --manifest-path crates/Cargo.toml -p geocoder-construction "
        "--bins --release" in value
    )


def test_pins_the_release_bbox_and_task_index_with_a_drift_check():
    doc = parsed()
    env = doc["env"]
    assert env["SLICE_RELEASE"] == "2026-07-22.0"
    assert env["SLICE_BBOX"] == "7.36 43.71 7.47 43.78"
    assert env["SLICE_TASK_INDEX"] == "33"
    value = text()
    # A release whose row-group layout moved must fail loudly, not silently run
    # a different task than the pinned one.
    assert 'if [ "$TASK" != "$SLICE_TASK_INDEX" ]' in value
    assert "update SLICE_TASK_INDEX" in value


def test_asserts_the_finalize_reconciliation_rather_than_mere_exit_status():
    value = text()
    assert ".reconciles == true and .records > 0" in value
    assert "did not reconcile" in value


def test_installs_only_the_hash_pinned_dependencies_on_the_hosted_python():
    value = text()
    doc = parsed()
    assert (
        "python -m pip install --only-binary=:all: --require-hashes "
        "-r .github/requirements-hosted-rowgroup.txt" in value
    )
    assert "actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0" in value
    assert "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1" in value
    assert "persist-credentials: false" in value
    for job in doc["jobs"].values():
        pythons = [
            step["with"]["python-version"]
            for step in job["steps"]
            if "setup-python" in step.get("uses", "")
        ]
        assert pythons == ["3.11.14"], pythons


def test_both_jobs_are_bounded_and_fast():
    doc = parsed()
    jobs = doc["jobs"]
    assert set(jobs) == {"hosted-imports", "slice"}
    for name, job in jobs.items():
        assert job["timeout-minutes"] <= 20, name


def test_the_import_check_covers_every_hosted_workflow_entrypoint():
    module = load_checker()
    modules = module.discover()
    # Derived from the hosted workflows, so a new invocation is covered for free.
    for name in (
        "construction_v1_hosted",
        "construction_v1_control",
        "project_places_construction_v1",
        "experiment_hosted_rowgroups",
        "places_construction_v1",
        "build_slice_inventory_v1",
    ):
        assert name in modules, name
    # Modules that parse arguments at import time cannot be imported; the slice
    # job runs them instead.
    assert "run_slice_construction_v1" not in modules
    for name in modules:
        assert (ROOT / "scripts" / f"{name}.py").exists(), name


def test_the_import_check_actually_imports_them():
    module = load_checker()
    assert module.main([]) == 0
