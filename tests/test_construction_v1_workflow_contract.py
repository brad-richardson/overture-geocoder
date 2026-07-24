"""Contract test pinning the construction-v1 hosted workflow.

The workflow must never become a false or unsafe dispatch surface: it stays
dispatch-only, gates on the typed EXECUTE_CONSTRUCTION_V1 confirmation in its
very first job, keeps every R2 write behind the execute-mode guard, pins its
action SHAs and hash-locked dependencies, and honours the map-matrix and
job-timeout caps from the scope document.
"""

from pathlib import Path

import yaml


WORKFLOW = (
    Path(__file__).parent.parent / ".github" / "workflows" / "construction-v1.yml"
)
REQUIREMENTS = (
    Path(__file__).parent.parent / ".github" / "requirements-hosted-rowgroup.txt"
)


def text() -> str:
    return WORKFLOW.read_text()


def parsed() -> dict:
    # `on` is the YAML boolean-key trap; PyYAML parses the bare `on:` key as
    # True, so look it up by that key.
    return yaml.safe_load(text())


def test_workflow_is_dispatch_only_and_non_publishing():
    value = text()
    trigger = value[value.index("on:") : value.index("permissions:")]
    assert "workflow_dispatch:" in trigger
    assert "pull_request:" not in trigger
    assert "push:" not in trigger
    assert "schedule:" not in trigger
    # Merging the PR must not trigger anything.
    doc = parsed()
    on = doc[True] if True in doc else doc["on"]
    assert list(on.keys()) == ["workflow_dispatch"]
    assert "contents: read" in value


def test_workflow_exposes_the_required_dispatch_inputs():
    doc = parsed()
    on = doc[True] if True in doc else doc["on"]
    inputs = on["workflow_dispatch"]["inputs"]
    assert inputs["family"]["options"] == ["address", "places"]
    assert inputs["mode"]["options"] == ["dry-run", "execute"]
    assert inputs["confirmation"]["required"] is True
    assert "resume_from" in inputs
    assert inputs["resume_from"]["required"] is False
    # admit-dispatch re-derives and byte-verifies the request; the SHA it checks
    # is parsed out of the confirmation, so the request bytes are an input too.
    assert "request_json" in inputs


def test_first_job_is_the_fail_closed_typed_confirmation_gate():
    value = text()
    doc = parsed()
    jobs = list(doc["jobs"].keys())
    assert jobs[0] == "admit"
    # The gate runs the control script's admit-dispatch, which regenerates the
    # canonical request and rejects a stale/mismatched SHA and any run_attempt
    # other than 1.
    assert "scripts/construction_v1_control.py admit-dispatch" in value
    assert "--run-attempt '${{ github.run_attempt }}'" in value
    assert "EXECUTE_CONSTRUCTION_V1" in value
    # The gate job must not read cloud credentials.
    admit = doc["jobs"]["admit"]
    admit_text = yaml.safe_dump(admit)
    assert "secrets." not in admit_text
    assert admit["if"] == "github.ref == 'refs/heads/main'"


def test_map_matrix_respects_concurrency_and_the_256_cap():
    value = text()
    doc = parsed()
    strategy = doc["jobs"]["map"]["strategy"]
    assert strategy["max-parallel"] == "${{ fromJSON(needs.admit.outputs.max_parallel) }}"
    assert strategy["fail-fast"] is False
    # The admission job refuses a matrix larger than the hosted 256-entry cap.
    assert "exceeds the 256 matrix cap" in value
    # Dry-run fans out only a bounded sample; execute uses the full matrix.
    assert (
        "inputs.mode == 'execute' && needs.admit.outputs.map_matrix "
        "|| needs.admit.outputs.sample_matrix" in value
    )


def test_every_phase_carries_the_330_minute_job_timeout():
    doc = parsed()
    jobs = doc["jobs"]
    assert jobs["map"]["timeout-minutes"] == 330
    assert jobs["reduce"]["timeout-minutes"] == 330
    assert jobs["head"]["timeout-minutes"] == 330
    # Finalization streams the whole slice; it stays under the 6h runner cap.
    assert jobs["finalize"]["timeout-minutes"] == 360
    assert jobs["finalize"]["timeout-minutes"] <= 360


def test_r2_writes_are_execute_mode_only():
    doc = parsed()
    jobs = doc["jobs"]
    # Execute-only phases are gated at the job level.
    for name in ("reduce", "head", "finalize"):
        assert jobs[name]["if"] == "inputs.mode == 'execute'"
    # The map job runs in both modes but every credentialed step is guarded.
    for step in jobs["map"]["steps"]:
        env = step.get("env", {}) or {}
        if any("secrets." in str(v) for v in env.values()):
            assert step.get("if") == "inputs.mode == 'execute'", step.get("name")


def test_workflow_pins_actions_and_hash_locked_dependencies():
    value = text()
    requirements = REQUIREMENTS.read_text()
    assert "actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0" in value
    assert "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1" in value
    assert "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a" in value
    assert (
        "jlumbroso/free-disk-space@54081f138730dfa15788a46383842cd2f914a1be" in value
    )
    assert "--require-hashes" in value
    assert "persist-credentials: false" in value
    # No unpinned floating action tags.
    assert "@main" not in value
    assert "@v7\n" not in value  # only the SHA-pinned "# v7" comment form
    assert "pyarrow==25.0.0" in requirements
    assert "duckdb==1.5.1" in requirements


def test_marker_written_last_and_fresh_dispatch_resume_are_pinned():
    value = text()
    assert "marker_written_last=true" in value
    assert "written last" in value
    assert "resume is a fresh dispatch" in value
    assert "never Re-run failed jobs" in value
    assert "NON-PROMOTING" in value
    # A runner ledger is seeded so prior runner minutes are honest on resume.
    assert "construction-v1-run-ledger-v1" in value
    assert "prior_runner_minutes" in value


def test_workflow_is_valid_yaml_with_a_connected_needs_graph():
    doc = parsed()
    jobs = doc["jobs"]
    known = set(jobs)
    for name, job in jobs.items():
        needs = job.get("needs", [])
        if isinstance(needs, str):
            needs = [needs]
        for dependency in needs:
            assert dependency in known, f"{name} needs unknown job {dependency}"
    assert jobs["finalize"]["needs"] == ["admit", "map", "reduce", "head"]
