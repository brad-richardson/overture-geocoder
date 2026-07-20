from pathlib import Path


ROOT = Path(__file__).parents[1]
PREFLIGHT = ROOT / ".github/workflows/preflight-global-v2-core.yml"
ID_SMOKE = ROOT / ".github/workflows/smoketest-r2-id.yml"


def test_core_preflight_is_manual_main_only_and_strictly_read_only():
    workflow = PREFLIGHT.read_text()
    trigger = workflow[workflow.index("on:") : workflow.index("permissions:")]
    assert "workflow_dispatch:" in trigger
    assert "push:" not in trigger
    assert "if: github.ref == 'refs/heads/main'" in workflow
    assert "persist-credentials: false" in workflow
    assert 'READ_ONLY_LEGACY_CORE::${LEGACY_CORE_VERSION}' in workflow
    assert "aws s3api get-object" in workflow
    assert "inspect-legacy-core" in workflow
    assert "read_only == true" in workflow
    for mutation in (
        "put-object",
        "delete-object",
        "aws s3 cp",
        "aws s3 rm",
        "wrangler r2 object put",
        "wrangler r2 object delete",
    ):
        assert mutation not in workflow


def test_id_smoke_allows_bounded_workers_dev_readiness_window():
    workflow = ID_SMOKE.read_text()
    query = workflow[
        workflow.index("      - name: Query current and historical v3 IDs") :
        workflow.index("      - name: Delete preview Worker", workflow.index("      - name: Query current and historical v3 IDs"))
    ]
    assert "for attempt in $(seq 1 12)" in query
    assert 'if [ "$attempt" -lt 12 ]; then' in query
    assert "sleep 10" in query
    assert "return 1" in query
    assert "--max-time 30" in query
