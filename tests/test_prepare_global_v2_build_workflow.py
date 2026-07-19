from pathlib import Path


WORKFLOW = (
    Path(__file__).parents[1]
    / ".github"
    / "workflows"
    / "prepare-global-v2-build.yml"
)


def text() -> str:
    return WORKFLOW.read_text()


def test_workflow_is_manual_main_only_and_has_no_data_plane_credentials():
    value = text()
    trigger = value[value.index("on:") : value.index("permissions:")]

    assert "workflow_dispatch:" in trigger
    assert "push:" not in trigger
    assert "pull_request:" not in trigger
    assert "schedule:" not in trigger
    assert "github.ref == 'refs/heads/main'" in value
    assert "contents: read" in value
    assert "id-token: write" not in value
    assert "secrets." not in value
    assert "aws s3" not in value
    assert "wrangler" not in value


def test_workflow_requires_prepare_only_and_reproduces_request():
    value = text()

    assert 'test "$CONFIRMATION" = PREPARE_ONLY' in value
    assert value.count("global_v2_build_request.py build") == 2
    assert value.count("global_v2_build_request.py validate") == 1
    assert "cmp build-request/request.json build-request/request-repeat.json" in value
    assert "No Overture object was read" in value
