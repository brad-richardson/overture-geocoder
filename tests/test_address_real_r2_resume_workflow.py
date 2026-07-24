import json
from pathlib import Path


ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "rehearse-address-r2-map-reduce.yml"
SELECTION = ROOT / ".github" / "address-sweep-selection.json"


def test_real_address_resume_is_manual_multi_task_non_promoting_and_cleans_up():
    workflow = WORKFLOW.read_text()
    trigger = workflow[workflow.index("on:") : workflow.index("permissions:")]

    assert "workflow_dispatch:" in trigger
    assert "push:" not in trigger
    assert "pull_request:" not in trigger
    assert "contents: read" in workflow
    assert "if: github.ref == 'refs/heads/main'" in workflow
    assert "persist-credentials: false" in workflow

    # The pre-existing stratified jobs are now gated to non-region dispatch so a
    # region run cannot perturb their byte-identical behavior.
    for job in ("select:", "real-address-resume:", "aggregate:"):
        block = workflow[workflow.index(f"  {job}") :]
        head = block[: block.index("steps:")]
        assert "inputs.mode != 'region'" in head, job

    # The matrix is now data-driven: the sweep fans out over the committed
    # stratified selection via fromJSON, not a hardcoded include list.
    assert "matrix: ${{ fromJSON(needs.select.outputs.matrix) }}" in workflow
    assert "max-parallel: 4" in workflow
    # Parallelism must not be raised above the fixed starting value of 4.
    assert "max-parallel: 8" not in workflow
    # The dispatch override is passed through an env var (validated as data by
    # the selection script), never interpolated into a shell command.
    assert "SWEEP_TASKS_OVERRIDE: ${{ inputs.tasks }}" in workflow
    assert "--override-json" in workflow
    assert "select_address_sweep_tasks.py check" in workflow

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

    # Every matrix task keeps its own run-unique, per-task R2 prefix and
    # cleanup, keyed on the matrix name.
    assert "${SHUFFLE_PREFIX}/${{ matrix.name }}" in workflow


def test_fan_in_aggregation_job_survives_partial_failure():
    workflow = WORKFLOW.read_text()

    # The aggregation job depends on the whole matrix, runs even when some
    # tasks fail, and emits a single summary artifact.
    assert "needs: [select, real-address-resume]" in workflow
    assert (
        "if: ${{ always() && github.ref == 'refs/heads/main'"
        " && inputs.mode != 'region' }}" in workflow
    )
    assert "scripts/aggregate_address_sweep.py" in workflow
    assert "address-sweep-aggregate-" in workflow
    # Download must not fail the job when only some tasks produced evidence.
    assert "continue-on-error: true" in workflow
    assert "pattern: address-resume-evidence-*" in workflow


def test_committed_selection_matrix_has_twelve_unique_stratified_tasks():
    selection = json.loads(SELECTION.read_text())
    tasks = selection["tasks"]

    assert selection["schema"] == "overture-address-sweep-selection-v1"
    assert len(tasks) == 12
    names = [t["name"] for t in tasks]
    indices = [t["task_index"] for t in tasks]
    assert len(set(names)) == 12
    assert len(set(indices)) == 12
    # At least one task per stratum named in the plan.
    strata = {t["stratum"] for t in tasks}
    assert strata == {
        "continuity-anchor",
        "cjk-japan",
        "cjk-traditional",
        "latin-high-density",
        "latin-europe",
        "sparse-tail",
        "mixed-unknown",
        "us-mid-range",
    }
    # Deterministic order: ascending task_index.
    assert indices == sorted(indices)
    # Names are artifact/shell safe.
    for name in names:
        assert name and all(c.islower() or c.isdigit() or c == "-" for c in name)


def test_region_mode_input_and_us_ne_defaults_present():
    workflow = WORKFLOW.read_text()
    assert "mode:" in workflow
    assert "type: choice" in workflow
    assert "- region" in workflow and "- stratified" in workflow
    # US-NE box defaults (lon -80.5..-66.9, lat 38.0..47.5).
    for default in ('"-80.5"', '"38.0"', '"-66.9"', '"47.5"'):
        assert default in workflow
    assert "max_region_tasks:" in workflow
    assert 'default: "40"' in workflow


def test_usa_mode_uses_the_pinned_complete_selection_and_ignores_overrides():
    workflow = WORKFLOW.read_text()

    assert "- usa" in workflow
    assert "select_address_sweep_tasks.py check-country" in workflow
    assert "--selection .github/address-usa-selection.json" in workflow
    assert '--country US' in workflow
    assert 'if [ "${RUN_MODE}" = "usa" ]; then' in workflow
    assert 'override=""' in workflow
    assert "selection_path: ${{ steps.pick.outputs.selection_path }}" in workflow
    assert 'SELECTION_PATH: ${{ needs.select.outputs.selection_path }}' in workflow
    assert '--selection "${SELECTION_PATH}"' in workflow


def test_region_plan_generates_scoped_inventory_and_enforces_hard_cap():
    workflow = WORKFLOW.read_text()
    plan = workflow[
        workflow.index("  plan-region:") : workflow.index("  region-address-resume:")
    ]
    assert "inputs.mode == 'region'" in plan
    # Run-time bbox-scoped inventory + task plan via the #106 flags.
    assert "scripts/inventory_address_rowgroups.py" in plan
    for flag in ("--xmin", "--ymin", "--xmax", "--ymax"):
        assert flag in plan
    # The whole scoped plan runs, but a plan above the cap FAILS the run. The
    # cap input reaches the CLI through an env var (not a direct ${{ }} splice
    # into the run block) so a crafted input cannot inject shell.
    assert "region_address_rehearsal.py matrix" in plan
    assert "MAX_REGION_TASKS: ${{ inputs.max_region_tasks }}" in plan
    assert '--max-tasks "${MAX_REGION_TASKS}"' in plan


def test_region_resume_reuses_map_reduce_machinery_over_scoped_plan():
    workflow = WORKFLOW.read_text()
    resume = workflow[
        workflow.index("  region-address-resume:") : workflow.index("  region-finalize:")
    ]
    # Same verified upload / resume / restore / stale-repair / cleanup machinery.
    for behavior in (
        "upload-manifest",
        "restore-manifest",
        "stale-restore.json",
        "resumed-upload.json",
        "overture-address-real-r2-resume-measurement-v1",
    ):
        assert behavior in resume
    # Tasks index into the run-time scoped inventory, not the committed one.
    assert "--inventory-report region-plan/scoped-inventory.json" in resume
    # The verified reduce output is published to the isolated family sub-prefix
    # and every task records its measured rows for reconciliation.
    assert 'FAMILY_PREFIX="${SHUFFLE_PREFIX}/family"' in resume
    assert "region_address_rehearsal.py task-rows" in resume
    # Per-task cleanup still removes only the task's own map fragments.
    assert 'TASK_PREFIX="${SHUFFLE_PREFIX}/${{ matrix.name }}"' in resume
    assert 'test "$REMAINING" = "0"' in resume


def test_region_finalize_reconciles_rows_and_verifies_family_manifest():
    workflow = WORKFLOW.read_text()
    finalize = workflow[workflow.index("  region-finalize:") :]
    # Success-gated (not always()) so a failed/missing task blocks the exact
    # reconciliation over the complete scoped task set.
    header = finalize[: finalize.index("steps:")]
    assert "needs: [plan-region, region-address-resume]" in header
    assert "if: github.ref == 'refs/heads/main' && inputs.mode == 'region'" in header
    assert "always()" not in header.replace("not always()", "")
    # Exact reconciliation of bbox_scoped_rows against measured map rows.
    assert "region_address_rehearsal.py reconcile" in finalize
    # Family manifest built and verified against the isolated-prefix listing.
    assert "region_address_rehearsal.py manifest" in finalize
    assert "list-objects-v2" in finalize and "family-listing.json" in finalize
    assert "manifest_digest" in finalize
    # The isolated family prefix is cleaned up afterward; no catalog write.
    assert 'aws s3 rm "s3://geocoder-shards/${FAMILY_PREFIX}/"' in finalize
    assert "catalog.json" not in finalize


def test_region_cleanup_sweeps_the_run_unique_prefix_on_every_path():
    workflow = WORKFLOW.read_text()
    cleanup = workflow[workflow.index("  region-cleanup:") :]
    header = cleanup[: cleanup.index("steps:")]
    # Runs even when a scoped task (and therefore region-finalize) fails, so the
    # family objects finalize would have deleted are not orphaned on failure.
    assert "needs: [plan-region, region-address-resume, region-finalize]" in header
    assert "always()" in header and "inputs.mode == 'region'" in header
    # Sweeps the WHOLE run-unique prefix, not just the family sub-prefix.
    assert 'aws s3 rm "s3://geocoder-shards/${SHUFFLE_PREFIX}/"' in cleanup
    assert 'test "$REMAINING" = "0"' in cleanup

