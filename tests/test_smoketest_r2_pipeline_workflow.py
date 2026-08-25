from pathlib import Path


WORKFLOW = (
    Path(__file__).parent.parent / ".github/workflows/smoketest-r2-pipeline.yml"
)


def test_shard_smoke_uses_latest_release_and_release_derived_identities():
    workflow = WORKFLOW.read_text()

    assert "Resolve latest Overture release" in workflow
    assert "python3 scripts/stac.py" in workflow
    assert "derive_monaco_smoke_contract.py" in workflow
    assert '--release "$OVERTURE_RELEASE"' in workflow
    assert '--output scripts/monaco_smoke_contract.json' in workflow
    assert "Resolve pinned Overture release" not in workflow
    assert "Verify pinned Monaco equivalence evidence" not in workflow
    assert "2026-07-12-monaco-subset-evidence.json" not in workflow
