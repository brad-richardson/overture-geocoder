from pathlib import Path


WORKFLOW = (
    Path(__file__).parent.parent / ".github" / "workflows" / "global-data-spike.yml"
)


def _text() -> str:
    return WORKFLOW.read_text()


def test_spike_is_manual_read_only_and_non_promoting():
    text = _text()
    trigger = text[text.index("on:") : text.index("permissions:")]

    assert "workflow_dispatch:" in trigger
    assert "pull_request:" not in trigger
    assert "push:" not in trigger
    assert "schedule:" not in trigger
    assert "contents: read" in text
    assert "id-token: write" not in text
    assert "secrets." not in text
    assert "promote" not in text.lower()
    assert "actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0" in text
    assert "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1" in text


def test_spike_is_bounded_to_standard_hosted_runners():
    text = _text()

    assert "runs-on: ubuntu-latest" in text
    assert "timeout-minutes: 20" in text
    assert "max-parallel: 4" in text
    assert "map_index: [0, 1, 2, 3]" in text
    assert "--map-tasks 4" in text
    assert "--partitions 8" in text


def test_spike_checks_determinism_and_reports_resources_without_data_plane_actions():
    text = _text()

    assert text.count("scripts/global_build_manifest.py plan") == 2
    assert text.count("scripts/global_build_manifest.py describe-task") == 1
    assert 'cmp "$first" "$second"' in text
    for command in ("lscpu", "free -h", "df -h", "/usr/bin/time -v"):
        assert command in text
    assert "actions/upload-artifact" not in text
    assert "actions/cache" not in text
    assert "short-lived, prefix-scoped R2 credentials" in text


def test_smoke_uses_one_matching_static_fixture_identity():
    text = _text()

    assert "${{ inputs." not in text
    assert 'SPIKE_RELEASE: "2026-07-02.3"' in text
    assert "SPIKE_DATASET: addresses" in text
    assert '[[ "$SPIKE_RELEASE" =~' in text
    assert "does not measure source I/O, shuffle, reduce, R2" in text
