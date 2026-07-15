from pathlib import Path


WORKFLOW = (
    Path(__file__).parent.parent
    / ".github"
    / "workflows"
    / "hosted-rowgroup-data-spike.yml"
)
REQUIREMENTS = WORKFLOW.parent.parent / "requirements-hosted-rowgroup.txt"


def text() -> str:
    return WORKFLOW.read_text()


def test_workflow_is_manual_read_only_and_non_publishing():
    value = text()
    trigger = value[value.index("on:") : value.index("permissions:")]
    assert "workflow_dispatch:" in trigger
    assert "pull_request:" not in trigger
    assert "push:" not in trigger
    assert "contents: read" in value
    assert "id-token: write" not in value
    assert "secrets." not in value
    assert "actions/upload-artifact" not in value
    assert "wrangler" not in value.lower()
    assert "aws s3" not in value.lower()


def test_workflow_has_hard_resource_and_data_limits():
    value = text()
    assert "timeout-minutes: 90" in value
    assert "--max-object-bytes 900000000" in value
    assert "--target-rowgroup-uncompressed-bytes 350000000" in value
    assert "--max-rows 3750000" in value
    assert "--max-groups 64" in value
    assert "--max-output-bytes 400000000" in value
    assert "--max-workspace-bytes 12000000000" in value
    assert "--max-artifact-bytes 1000000000" in value


def test_workflow_pins_actions_and_dependency():
    value = text()
    requirements = REQUIREMENTS.read_text()
    assert "actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0" in value
    assert "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1" in value
    assert "pyarrow==25.0.0" in requirements
    assert "numpy==2.3.5" in requirements
    assert requirements.count("--hash=sha256:") == 2
    assert "--require-hashes" in value
    assert "persist-credentials: false" in value


def test_workflow_uses_current_release_and_ephemeral_output():
    value = text()
    assert "--release 2026-06-17.0" in value
    assert "--output /tmp/address-rowgroups.parquet" in value
    assert "--output /tmp/address-reduced.aidx" in value
    assert "No artifact was uploaded and no catalog or production state was written." in value


def test_workflow_runs_bounded_reduce_without_a_remote_data_plane():
    value = text()
    assert "scripts/experiment_address_reduce.py" in value
    assert "--fragment-rows 128000" in value
    assert "--sparse-stride 256" in value
    assert "R2 shuffle latency is not measured" in value
