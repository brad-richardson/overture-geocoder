"""Structural contract for the manual read-only Address reverse probe."""

from __future__ import annotations

import re
from pathlib import Path

import yaml


ROOT = Path(__file__).parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "reverse-address-probe.yml"


def load():
    return yaml.safe_load(WORKFLOW.read_text())


def body() -> str:
    return "\n".join(
        step.get("run") or "" for step in load()["jobs"]["probe"]["steps"]
    )


def test_probe_is_manual_main_only_and_addresses_only():
    workflow = load()
    assert set(workflow[True]) == {"workflow_dispatch"}
    inputs = workflow[True]["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"plan_run_id", "plan_sha256"}
    assert "github.ref == 'refs/heads/main'" in workflow["jobs"]["probe"]["if"]
    assert workflow["permissions"] == {"contents": "read", "actions": "read"}
    assert workflow["concurrency"]["cancel-in-progress"] is False
    assert "family" not in inputs


def test_probe_authenticates_exact_successful_dry_run_plan():
    script = body()
    for token in (
        '.name == "Build v2 reverse indexes"',
        '.path == ".github/workflows/reverse-v2.yml"',
        '.head_branch == "main"',
        '.conclusion == "success"',
        "reverse-v2-plan-${PLAN_RUN_ID}-${RUN_ATTEMPT}",
        "sha256sum plan/out/reverse-plan.json",
        '.mode == "dry-run"',
        '.state == "fresh"',
        '.family == "addresses"',
        ".slice_claim == null",
    ):
        assert token in script


def test_probe_invokes_local_measurement_with_hard_caps_and_no_r2_writes():
    script = body()
    assert "probe_reverse_address_v1.py" in script
    assert '--max-output-bytes "$PROBE_MAX_OUTPUT_BYTES"' in script
    assert '--wall-seconds "$PROBE_WALL_SECONDS"' in script
    assert "--staging-bucket" in script
    for forbidden in (
        "--publish-destination",
        "put-object",
        "delete-object",
        "write_marker",
        "promote",
        "catalog.py",
    ):
        assert forbidden not in script.lower()


def test_every_action_is_sha_pinned():
    for step in load()["jobs"]["probe"]["steps"]:
        uses = step.get("uses")
        if uses:
            assert re.search(r"@[0-9a-f]{40}$", uses), uses
