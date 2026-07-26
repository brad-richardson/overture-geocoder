"""Contract for the one-object live R2 publication proof."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/r2-publication-probe.yml"


def load():
    return yaml.safe_load(WORKFLOW.read_text())


def test_probe_is_manual_main_only_and_read_only():
    workflow = load()
    assert set(workflow[True]) == {"workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read"}
    job = workflow["jobs"]["probe"]
    assert job["if"] == "github.ref == 'refs/heads/main'"
    assert job["timeout-minutes"] == 10


def test_probe_uses_the_production_backend_for_one_isolated_key():
    workflow = WORKFLOW.read_text()
    assert (
        "PROBE_KEY: construction-v1/probes/r2-publication/"
        "${{ github.run_id }}-${{ github.run_attempt }}.bin"
    ) in workflow
    assert "hosted._publication_remote(args, budget)" in workflow
    assert "remote.put_create_only_stream(" in workflow
    assert "first.store.head_proof(key)" in workflow
    assert "identical-conflict-accepted" in workflow
    assert "different bytes were accepted under an existing key" in workflow


def test_cleanup_is_unconditional_exact_key_only_and_fail_closed():
    workflow = load()
    cleanup = workflow["jobs"]["probe"]["steps"][-1]
    assert cleanup["if"] == "always()"
    script = cleanup["run"]
    assert '--key "$PROBE_KEY"' in script
    assert '--prefix "$PROBE_KEY"' in script
    assert "--recursive" not in script
    assert "exit 1" in script
