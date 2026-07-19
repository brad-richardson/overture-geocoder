from pathlib import Path


ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "usa-scale-signal.yml"


def test_usa_scale_signal_is_manual_confirmed_main_only_and_non_promoting():
    workflow = WORKFLOW.read_text()
    trigger = workflow[workflow.index("on:") : workflow.index("permissions:")]

    assert "workflow_dispatch:" in trigger
    assert "push:" not in trigger
    assert "pull_request:" not in trigger
    assert "confirm:" in trigger
    assert 'if [ "${CONFIRM}" != "USA" ]; then' in workflow
    assert "if: github.ref == 'refs/heads/main'" in workflow
    assert "persist-credentials: false" in workflow
    assert "cancel-in-progress: false" in workflow
    assert "catalog.json" not in workflow
    assert "releases/" not in workflow


def test_usa_scale_signal_calls_both_existing_isolated_workflows():
    workflow = WORKFLOW.read_text()

    assert "uses: ./.github/workflows/build-places-region.yml" in workflow
    assert "uses: ./.github/workflows/rehearse-address-r2-map-reduce.yml" in workflow
    assert 'bbox_xmin: "-125.0"' in workflow
    assert 'bbox_ymin: "24.4"' in workflow
    assert 'bbox_xmax: "-66.9"' in workflow
    assert 'bbox_ymax: "49.4"' in workflow
    assert 'extract_limit: "22000000"' in workflow
    assert "mode: usa" in workflow
    assert workflow.count("cleanup: true") == 1
    assert "secrets: inherit" not in workflow
    assert workflow.count("R2_ACCESS_KEY_ID: ${{ secrets.R2_ACCESS_KEY_ID }}") == 2
    assert workflow.count("R2_SECRET_ACCESS_KEY: ${{ secrets.R2_SECRET_ACCESS_KEY }}") == 2
    assert workflow.count("CLOUDFLARE_ACCOUNT_ID: ${{ secrets.CLOUDFLARE_ACCOUNT_ID }}") == 2


def test_usa_selection_and_combined_report_are_fail_closed_and_retained():
    workflow = WORKFLOW.read_text()

    assert "select_address_sweep_tasks.py check-country" in workflow
    assert ".task_count == 33" in workflow
    assert ".projected_rows == 130996768" in workflow
    assert ".exact_country_export == false" in workflow
    assert "scripts/usa_scale_report.py" in workflow
    assert "places-region-evidence-${{ github.run_id }}-${{ github.run_attempt }}" in workflow
    assert "address-sweep-aggregate-${{ github.run_id }}-${{ github.run_attempt }}" in workflow
    assert "usa-scale-report-${{ github.run_id }}-${{ github.run_attempt }}" in workflow
    assert "retention-days: 90" in workflow


def test_called_workflows_expose_reusable_contracts():
    places = (ROOT / ".github" / "workflows" / "build-places-region.yml").read_text()
    addresses = (
        ROOT / ".github" / "workflows" / "rehearse-address-r2-map-reduce.yml"
    ).read_text()

    for workflow in (places, addresses):
        trigger = workflow[workflow.index("on:") : workflow.index("permissions:")]
        assert "workflow_call:" in trigger
        for secret in (
            "R2_ACCESS_KEY_ID:",
            "R2_SECRET_ACCESS_KEY:",
            "CLOUDFLARE_ACCOUNT_ID:",
        ):
            assert secret in trigger
