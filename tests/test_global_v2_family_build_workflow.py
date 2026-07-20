import re
from pathlib import Path


ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github/workflows/global-v2-family-build.yml"
PREVIEW_CONFIG = (
    ROOT / "crates/geocoder-worker/wrangler.global-v2-preview.toml"
)


def text() -> str:
    return WORKFLOW.read_text()


def test_workflow_is_manual_main_only_typed_and_bounded():
    value = text()
    trigger = value[value.index("on:") : value.index("permissions:")]
    assert "workflow_dispatch:" in trigger
    assert "push:" not in trigger
    assert "pull_request:" not in trigger
    assert "schedule:" not in trigger
    assert "github.ref == 'refs/heads/main'" in value
    assert "options: [dry-run, execute]" in value
    assert len(re.findall(r"^      [a-z_]+:$", trigger, re.MULTILINE)) <= 10
    assert "cancel-in-progress: false" in value
    assert "max-parallel: ${{ fromJSON(inputs.max_parallel) }}" in value
    assert 'python-version: "3.11.14"' in value
    assert "--require-hashes" in value
    assert "--only-binary=:all:" in value
    assert "--max-total-runner-minutes" in value
    assert "--max-estimated-cost-usd" in value
    trusted = value[value.index("  validate-request:") : value.index("  preflight:")]
    assert "secrets." not in trusted
    assert "git show -s --format=%cI" in trusted
    assert "--runner-image-os ubuntu24 --runner-image-version per-job-provenance" in trusted
    assert "date -u" not in value
    assert "fetch-depth: 0" in trusted
    assert value.index("  validate-request:") < value.index("fromJSON(inputs.request_json).producer_commit")


def test_workflow_uses_real_resumable_boundaries_and_dynamic_matrices():
    value = text()
    for command in (
        "publish-inventory",
        "publish-map",
        "complete-phase",
        "restore-map-planner-inputs",
        "build-publish-plans",
        "restore-reducer-plans",
        "run-publish-reduce",
        "run-publish-head",
        "restore-finalization-reports",
        "finalize-publish-families",
        "publish-global-v2-slice",
        "publish-preview",
        "publish-worker-smoke-evidence",
    ):
        assert command in value
    assert ".plan.tasks[]" in value
    assert ".map_plan.tasks[]" in value
    assert ".matrix" in value
    assert "address-reduce-job-" not in value
    assert "task_id:.id" in value
    assert "phase-evidence/inventory.json" in value
    assert "phase-evidence/head.json" in value
    assert "one-pass" in value.lower() or "one whole-slice" in value.lower()
    assert "inputs.mode == 'execute'" in value
    assert "execution-budget" in value
    assert "--consumed-runner-minutes" in value
    assert "--prior-runner-minutes" in value
    assert value.count("--run-attempt '${{ github.run_attempt }}'") == value.count(
        "global_v2_executor.py preflight"
    )
    assert "resume only with a fresh dispatch, never Re-run jobs" in value
    assert "MAX_TOTAL_RUNNER_MINUTES" in value or "budget limits" in value
    assert "admit-task" in value
    assert "needs: [preflight, address-map]" in value
    assert "  finalize-slice:" in value
    assert "  worker-smoke:" in value
    assert "needs: [preflight, aggregate-plan, finalize-slice]" in value
    assert "estimated_total_runner_minutes" in value
    assert "Combined reducer tasks" in value


def test_actions_disk_and_preview_dependencies_are_exactly_pinned():
    value = text()
    uses = re.findall(r"^\s*uses:\s*(\S+)", value, re.MULTILINE)
    assert uses
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", item) for item in uses)
    assert "jlumbroso/free-disk-space@54081f138730dfa15788a46383842cd2f914a1be" in value
    assert "wrangler@4.112.0" in value
    assert "worker-build --version 0.7.2 --locked" in value
    assert "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a" in value
    assert value.count("df -Pk / | awk") >= 6


def test_preview_is_workers_dev_only_and_cleanup_is_exact():
    value = text()
    preview = PREVIEW_CONFIG.read_text()
    assert "workers_dev = true" in preview
    assert "routes" not in preview
    assert 'ENVIRONMENT = "preview"' in preview
    assert "wrangler.global-v2-preview.toml" in value
    assert "V2_CATALOG_KEY_OVERRIDE" in value
    assert "for ATTEMPT in $(seq 1 12)" in value
    assert "sleep 10" in value
    assert "aws s3api delete-object" in value
    assert "aws s3 rm" not in value
    assert "--recursive" not in value
    assert "remaining_object_keys:$remaining" in value
    assert "production_catalog_writes:false" in value
    assert 'CATALOG_KEY="smoketest-v2/' in value
    assert 'RELEASE_KEY="smoketest-v2/' in value
    assert "types=poi" in value
    assert "smoke_sample.query" in value
    assert "smoke_sample.street" in value
    assert "control/places-query.json" in value
    assert "control/address-query.json" in value
    assert "control/smoke-requests.json" in value
    assert "EXPECTED_GEOCODER_BUILD" in value
    assert "EXPECTED_OVERTURE_RELEASE" in value
    assert "EXPECTED_V2_CATALOG_KEY" in value
    assert "-X DELETE" in value
    assert "FAILED=0" in value


def test_secrets_are_step_scoped_and_never_reach_setup_or_install_actions():
    value = text()
    assert not re.search(r"^    env:\n(?:      .*\n)*?      .*secrets\.", value, re.MULTILINE)
    for marker in (
        "actions/checkout@",
        "actions/setup-python@",
        "jlumbroso/free-disk-space@",
        "python -m pip install",
        "cargo install worker-build",
        "npm install --global",
    ):
        line = next(line for line in value.splitlines() if marker in line)
        assert "secrets." not in line


def test_workflow_never_invokes_a_production_catalog_mutation():
    value = text()
    for forbidden in (
        'put-object --key "catalog.json"',
        'put-object --key "v2/catalog.json"',
        'delete-object --key "catalog.json"',
        'delete-object --key "v2/catalog.json"',
        "v2/releases/",
        "finalize_rebuild.py promote",
    ):
        assert forbidden not in value
