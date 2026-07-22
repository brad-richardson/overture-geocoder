from pathlib import Path


ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "smoketest-r2-shuffle.yml"


def test_verified_shuffle_smoke_is_merge_only_non_promoting_and_cleans_up():
    workflow = WORKFLOW.read_text()
    trigger = workflow[workflow.index("on:") : workflow.index("permissions:")]

    assert "workflow_dispatch:" in trigger
    assert "push:" in trigger
    assert "branches: [main]" in trigger
    assert "pull_request:" not in trigger
    assert "scripts/r2_verified_store.py" in trigger
    assert "scripts/r2_fragment_fetch.py" in trigger
    assert "contents: read" in workflow
    assert (
        "SHUFFLE_PREFIX: smoke/r2-shuffle/${{ github.run_id }}-${{ github.run_attempt }}"
        in workflow
    )
    assert "upload-manifest" in workflow
    assert "restore-manifest" in workflow
    assert "partial.json" in workflow
    assert "resumed-upload.json" in workflow
    assert "stale-restore.json" in workflow
    assert 'python-version: "3.11.14"' in workflow
    assert "requirements-hosted-rowgroup.txt" in workflow
    assert "materialize_selected_row_groups" in workflow
    assert 'row_groups=[1, 3]' in workflow
    assert '"full_object_reads": full_object_reads' in workflow
    assert "if not reads or full_object_reads:" in workflow
    assert "selective-metrics.json" in workflow
    assert "s3://geocoder-shards/${SHUFFLE_PREFIX}/" in workflow
    assert 'test "$REMAINING" = "0"' in workflow
    assert "catalog" not in workflow.lower()
