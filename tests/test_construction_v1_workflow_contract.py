"""Contract test pinning the construction-v1 hosted workflow.

The workflow must never become a false or unsafe dispatch surface: it stays
dispatch-only, gates on the typed EXECUTE_CONSTRUCTION_V1 confirmation in its
very first job, derives the contract/runtime every later job consumes, passes
phase state between jobs as pinned artifacts, keeps every R2 write behind the
execute-mode guard, and ENFORCES the runner-minute ledger (sum + abort) rather
than merely printing it.
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
    return yaml.safe_load(text())


def test_workflow_is_dispatch_only_and_non_publishing():
    value = text()
    trigger = value[value.index("on:") : value.index("permissions:")]
    assert "workflow_dispatch:" in trigger
    assert "pull_request:" not in trigger
    assert "push:" not in trigger
    assert "schedule:" not in trigger
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
    assert inputs["resume_from"]["required"] is False
    assert "request_json" in inputs


def test_first_job_is_the_fail_closed_gate_that_derives_the_contract():
    value = text()
    doc = parsed()
    jobs = list(doc["jobs"].keys())
    assert jobs[0] == "admit"
    assert "scripts/construction_v1_control.py admit-dispatch" in value
    assert "--run-attempt '${{ github.run_attempt }}'" in value
    assert "EXECUTE_CONSTRUCTION_V1" in value
    # MAJOR-1: contract.json / runtime.json are actually generated (nothing
    # downstream can reference a file that was never produced).
    assert "scripts/construction_v1_hosted.py derive-contract" in value
    assert "--output control/contract.json --runtime control/runtime.json" in value
    admit = doc["jobs"]["admit"]
    assert "secrets." not in yaml.safe_dump(admit)
    assert admit["if"] == "github.ref == 'refs/heads/main'"


def test_execute_data_plane_uses_the_native_adapter_not_a_missing_contract():
    value = text()
    for command in ("run-map", "plan-reduce", "run-reduce", "run-head", "finalize"):
        assert f"scripts/construction_v1_hosted.py {command}" in value
    # It must NOT depend on the global_v2 contract this workflow never produces.
    assert "global_v2_hosted.py" not in value
    assert "control/runtime.json" in value


def test_phase_outputs_flow_between_jobs_as_pinned_artifacts():
    value = text()
    doc = parsed()
    assert "actions/download-artifact@37930b1c2abaa49bbe596cd826c3c89aef350131" in value
    assert "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a" in value
    assert "merge-multiple: true" in value
    assert (
        doc["jobs"]["reduce"]["strategy"]["matrix"]
        == "${{ fromJSON(needs.plan.outputs.reduce_matrix) }}"
    )


def test_map_matrix_respects_concurrency_and_the_256_cap():
    value = text()
    doc = parsed()
    strategy = doc["jobs"]["map"]["strategy"]
    assert strategy["max-parallel"] == "${{ fromJSON(needs.admit.outputs.max_parallel) }}"
    assert strategy["fail-fast"] is False
    assert "exceeds the 256 matrix cap" in value
    assert "reduce matrix exceeds 256 cap" in value
    assert (
        "inputs.mode == 'execute' && needs.admit.outputs.map_matrix "
        "|| needs.admit.outputs.sample_matrix" in value
    )


def test_every_phase_carries_the_330_minute_job_timeout():
    doc = parsed()
    jobs = doc["jobs"]
    for name in ("map", "plan", "reduce", "head"):
        assert jobs[name]["timeout-minutes"] == 330
    assert jobs["finalize"]["timeout-minutes"] == 360


def test_r2_writes_are_execute_mode_only_and_create_only():
    value = text()
    doc = parsed()
    jobs = doc["jobs"]
    for name in ("plan", "reduce", "head", "finalize"):
        assert jobs[name]["if"] == "inputs.mode == 'execute'"
    for name in ("admit", "map"):
        for step in jobs[name]["steps"]:
            env = step.get("env", {}) or {}
            if any("secrets." in str(v) for v in env.values()):
                assert step.get("if") == "inputs.mode == 'execute'", (name, step.get("name"))
    assert "--if-none-match '*'" in value
    assert "marker written last" in value.lower()


def test_runner_minute_ledger_is_enforced_not_decorative():
    value = text()
    doc = parsed()
    # MAJOR-2: the ledger is summed and the run FAILS CLOSED before the next
    # phase (ledger-check exits non-zero when projected > cap, proven in
    # tests/test_construction_v1_hosted.py::test_ledger_fails_closed...).
    assert "scripts/construction_v1_hosted.py ledger-check" in value
    assert "scripts/construction_v1_hosted.py ledger-append" in value
    assert "--next-phase-minutes" in value
    plan_block = value[value.index("Fan in map minutes") : value.index("Publish the plan")]
    assert "ledger-append" in plan_block
    assert "ledger-check" in plan_block
    assert plan_block.index("ledger-check") < plan_block.index("reduce_matrix=")
    assert "reduce_matrix" in doc["jobs"]["plan"]["outputs"]


def test_resume_carries_the_prior_ledger_or_fails_closed():
    value = text()
    doc = parsed()
    # MAJOR-3: resume downloads the prior run's ledger read-only and refuses to
    # start on a mismatch, so a fresh dispatch cannot silently reset prior spend.
    assert doc["permissions"]["actions"] == "read"
    assert "actions/runs/${RESUME_FROM}/artifacts" in value
    assert "resume failed closed" in value
    assert "construction-v1-ledger-" in value
    assert "!= confirmation PRIOR_RUNNER_MINUTES" in value


def test_workflow_pins_actions_and_hash_locked_dependencies():
    value = text()
    requirements = REQUIREMENTS.read_text()
    assert "actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0" in value
    assert "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1" in value
    assert "--require-hashes" in value
    assert "persist-credentials: false" in value
    assert "@main" not in value
    assert "@v7\n" not in value
    assert "@v5" not in value
    assert "pyarrow==25.0.0" in requirements
    assert "duckdb==1.5.1" in requirements


def test_marker_written_last_and_fresh_dispatch_resume_are_pinned():
    value = text()
    assert "marker_written_last=true" in value
    assert "NON-PROMOTING" in value
    assert "never Re-run failed jobs" in value
    assert "construction-v1-run-ledger-v1" in value


def test_needs_graph_is_connected():
    doc = parsed()
    jobs = doc["jobs"]
    known = set(jobs)
    for name, job in jobs.items():
        needs = job.get("needs", [])
        if isinstance(needs, str):
            needs = [needs]
        for dependency in needs:
            assert dependency in known, f"{name} needs unknown job {dependency}"
    assert jobs["finalize"]["needs"] == ["admit", "plan", "reduce", "head"]
    assert jobs["reduce"]["needs"] == ["admit", "plan"]
