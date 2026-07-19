import re
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
    assert len(re.findall(r"^      [a-z_]+:$", trigger, re.MULTILINE)) <= 10


def test_workflow_requires_prepare_only_and_reproduces_request():
    value = text()

    assert 'test "$CONFIRMATION" = PREPARE_ONLY' in value
    assert value.count("global_v2_build_request.py build") == 2
    assert value.count("global_v2_build_request.py validate") == 1
    assert "cmp build-request/request.json build-request/request-repeat.json" in value
    assert "No Overture object was read" in value


def test_workflow_freezes_source_core_slice_and_predecessor_inputs():
    value = text()
    trigger = value[value.index("on:") : value.index("permissions:")]

    for required in (
        "slice_version",
        "legacy_core_version",
        "legacy_core_manifest_sha256",
        "family_inputs_json",
    ):
        assert f"      {required}:" in trigger
    assert "inputs.addresses_inventory_sha256" not in trigger
    assert "inputs.places_inventory_sha256" not in trigger
    assert '(keys == ["addresses", "places"])' in value
    assert 'jq -r \'.addresses.inventory_sha256\'' in value
    assert 'jq -r \'.places.inventory_sha256\'' in value
    assert "--legacy-core-overture-release \"$OVERTURE_RELEASE\"" in value
    assert "--legacy-core-manifest-key \"$LEGACY_CORE_MANIFEST_KEY\"" in value
    assert "--addresses-predecessor-family-manifest-sha256" in value
    assert "--places-predecessor-family-manifest-sha256" in value
