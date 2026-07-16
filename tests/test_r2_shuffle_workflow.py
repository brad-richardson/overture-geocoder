from pathlib import Path


ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "smoketest-r2-shuffle.yml"


def test_verified_shuffle_smoke_is_manual_non_promoting_and_cleans_up():
    workflow = WORKFLOW.read_text()
    trigger = workflow[workflow.index("on:") : workflow.index("permissions:")]

    assert "workflow_dispatch:" in trigger
    assert "push:" not in trigger
    assert "contents: read" in workflow
    assert "upload-manifest" in workflow
    assert "restore-manifest" in workflow
    assert "partial.json" in workflow
    assert "resumed-upload.json" in workflow
    assert "stale-restore.json" in workflow
    assert "s3://geocoder-shards/${SHUFFLE_PREFIX}/" in workflow
    assert 'test "$REMAINING" = "0"' in workflow
    assert "catalog" not in workflow.lower()
