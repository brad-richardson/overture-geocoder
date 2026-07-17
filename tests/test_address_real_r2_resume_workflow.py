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
    assert "persist-credentials: false" in workflow
    assert "task_index: 48" in workflow
    assert "task_index: 3" in workflow
    assert "benchmarks/address-rowgroup-inventory-report.json" in workflow
    assert "scripts/prepare_address_verified_resume.py map" in workflow
    assert "scripts/prepare_address_verified_resume.py reduce" in workflow
    assert "scripts/r2_verified_store.py" in workflow
    assert "partial-upload.json" in workflow
    assert "resumed-upload.json" in workflow
    assert "empty-restore.json" in workflow
    assert "stale-restore.json" in workflow
    assert "--expected-report" in workflow
    assert "local_oracle_match == true" in workflow
    assert "overture-address-real-r2-resume-measurement-v1" in workflow
    assert "retry_readback_amplification" in workflow
    assert "smoke/address-real-shuffle/" in workflow
    assert "catalog" in workflow.lower()
    assert "cannot update a catalog" in workflow
    assert "aws s3 rm" in workflow
    assert 'test "$REMAINING" = "0"' in workflow


def test_large_real_data_rehearsal_does_not_replace_small_merge_smoke():
    small = (ROOT / ".github" / "workflows" / "smoketest-r2-shuffle.yml").read_text()
    trigger = small[small.index("on:") : small.index("permissions:")]

    assert "push:" in trigger
    assert "branches: [main]" in trigger
    assert "fragment-${index}.bin" in small
    assert "experiment_hosted_rowgroups.py" not in small
