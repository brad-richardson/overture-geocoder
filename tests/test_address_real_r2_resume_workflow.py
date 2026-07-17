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
    assert "if: ${{ always() && github.ref == 'refs/heads/main' }}" in workflow
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


def test_large_real_data_rehearsal_does_not_replace_small_merge_smoke():
    small = (ROOT / ".github" / "workflows" / "smoketest-r2-shuffle.yml").read_text()
    trigger = small[small.index("on:") : small.index("permissions:")]

    assert "push:" in trigger
    assert "branches: [main]" in trigger
    assert "fragment-${index}.bin" in small
    assert "experiment_hosted_rowgroups.py" not in small
