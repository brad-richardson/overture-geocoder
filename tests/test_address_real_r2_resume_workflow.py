from pathlib import Path


ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "rehearse-address-r2-map-reduce.yml"


def test_real_address_resume_is_manual_multi_task_non_promoting_and_cleans_up():
    workflow = WORKFLOW.read_text()
    trigger = workflow[workflow.index("on:") : workflow.index("permissions:")]

    assert "workflow_dispatch:" in trigger
    assert "push:" not in trigger
    assert "pull_request:" not in trigger
    assert "contents: read" in workflow
    assert "if: github.ref == 'refs/heads/main'" in workflow
    assert "persist-credentials: false" in workflow
    assert workflow.count("task_index:") == 2
    for required_behavior in (
        "scripts/prepare_address_verified_resume.py map",
        "scripts/prepare_address_verified_resume.py reduce",
        "scripts/r2_verified_store.py",
        "--expected-report",
        "local_oracle_match == true",
        "overture-address-real-r2-resume-measurement-v1",
        "cannot update a catalog",
        'test "$REMAINING" = "0"',
    ):
        assert required_behavior in workflow


def test_large_real_data_rehearsal_does_not_replace_small_merge_smoke():
    small = (ROOT / ".github" / "workflows" / "smoketest-r2-shuffle.yml").read_text()
    trigger = small[small.index("on:") : small.index("permissions:")]

    assert "push:" in trigger
    assert "branches: [main]" in trigger
    assert "fragment-${index}.bin" in small
    assert "experiment_hosted_rowgroups.py" not in small
