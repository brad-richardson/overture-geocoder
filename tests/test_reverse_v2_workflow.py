"""Structural contract for the manual, non-promoting reverse-v2 workflow."""

from __future__ import annotations

import re
from pathlib import Path

import yaml


ROOT = Path(__file__).parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "reverse-v2.yml"


def load():
    return yaml.safe_load(WORKFLOW.read_text())


def jobs():
    return load()["jobs"]


def body(name: str) -> str:
    return "\n".join(step.get("run") or "" for step in jobs()[name]["steps"])


def test_dispatch_is_manual_main_only_and_exactly_scoped():
    workflow = load()
    assert set(workflow[True]) == {"workflow_dispatch"}
    inputs = workflow[True]["workflow_dispatch"]["inputs"]
    assert set(inputs) == {
        "family",
        "source",
        "slice_version",
        "mode",
        "max_parallel",
        "max_total_runner_minutes",
        "confirmation",
    }
    assert inputs["family"]["options"] == ["places", "addresses"]
    assert inputs["mode"]["options"] == ["dry-run", "execute"]
    assert inputs["mode"]["default"] == "dry-run"
    assert "github.ref == 'refs/heads/main'" in jobs()["plan"]["if"]


def test_reverse_writes_share_the_v2_publication_lock_and_never_cancel():
    concurrency = load()["concurrency"]
    assert concurrency == {
        "group": "r2-v2-publication",
        "cancel-in-progress": False,
    }


def test_execute_confirmation_binds_cost_shape_and_destination():
    script = body("plan")
    for token in (
        "EXECUTE_REVERSE_V2::",
        "::FAMILY=${FAMILY}",
        "::SLICE=${SLICE_VERSION}",
        "::RANGES=${RANGE_COUNT}",
        "::MAX_PARALLEL=${MAX_PARALLEL}",
        "::MAX_TOTAL_RUNNER_MINUTES=${MAX_TOTAL_RUNNER_MINUTES}",
        "::MAX_OUTPUT_BYTES_PER_RANGE=${REDUCE_MAX_OUTPUT_BYTES}",
    ):
        assert token in script
    assert 'if [ "$MODE" = execute ] && [ "$CONFIRMATION" != "$EXPECTED" ]' in script
    assert load()["env"]["EXECUTE_MAX_RUNNER_MINUTES"] == "930"
    assert load()["env"]["REDUCE_WALL_SECONDS"] == "2400"
    assert load()["env"]["REDUCE_MAX_OUTPUT_BYTES"] == "3221225472"
    assert '16 range jobs * 45' in WORKFLOW.read_text()
    assert (
        '[ "$MAX_TOTAL_RUNNER_MINUTES" -lt "$EXECUTE_MAX_RUNNER_MINUTES" ]'
        in script
    )


def test_plan_regenerates_exact_admitted_tasks_then_streams_the_compact_plan():
    script = body("plan")
    assert "reverse_r2_v1.py admit" in script
    assert "--publish-destination" in script
    assert '--mode "$MODE"' in script
    assert script.index("reverse_r2_v1.py admit") < script.index(
        "reverse_r2_v1.py plan"
    )
    assert "reverse_r2_v1.py plan" in script
    assert "--task-ids-file task-ids.txt" in script
    assert "--staging-bucket" in script
    assert "forward-finalize.json" in script
    assert "overture-construction-v1-create-only-marker-v1" in script
    assert "construction-v1-family-manifest-v1" in script
    assert "FAMILY_MANIFEST_SHA256" in script
    assert "sha256sum forward-family-manifest.json" in script
    assert "FAMILY_MANIFEST_BYTES" in script
    assert "wc -c < forward-family-manifest.json" in script
    assert "FORWARD_POSITIONS_RECORDS" in script
    assert 'test "$EXPECTED_RECORDS" = "$FORWARD_POSITIONS_RECORDS"' in script
    assert "construction_v1_control.py admit-dispatch" in script
    assert "--request control/request.json" in script
    assert "--run-attempt 1" in script
    assert "--github-output control/matrices.env" in script
    assert "MATRIX_KEY=places_matrix" in script
    assert "MATRIX_KEY=address_matrix" in script
    assert "list-objects-v2" not in script


def test_execute_is_sixteen_bounded_ranges_then_one_catalog():
    named = jobs()
    assert set(named) == {"plan", "reduce", "catalog"}
    assert "inputs.mode == 'execute'" in named["reduce"]["if"]
    assert named["reduce"]["strategy"]["max-parallel"] == (
        "${{ fromJSON(inputs.max_parallel) }}"
    )
    assert "range(0; 16)" in body("plan")
    reduce = body("reduce")
    assert "reverse_r2_v1.py reduce" in reduce
    assert "--publish-destination" in reduce
    assert "--plan plan/out/reverse-plan.json" in reduce
    assert '--wall-seconds "$REDUCE_WALL_SECONDS"' in reduce
    assert '--max-output-bytes "$REDUCE_MAX_OUTPUT_BYTES"' in reduce
    catalog = body("catalog")
    assert "reverse_r2_v1.py catalog" in catalog
    assert "reverse-v2-${{ inputs.family }}-catalog" in str(
        named["catalog"]["steps"]
    )


def test_workflow_never_reruns_forward_or_promotes_a_catalog():
    all_body = "\n".join(body(name) for name in jobs())
    for forbidden in (
        "construction_v1_hosted.py run-map",
        "address-transform-v1",
        "places-transform-v1",
        "v2_release_manifest.py promote",
        "promote_construction_slice.py execute",
        "delete-object",
        "aws s3 rm",
    ):
        assert forbidden not in all_body


def test_every_action_is_sha_pinned():
    for job in jobs().values():
        for step in job.get("steps", []):
            uses = step.get("uses")
            if uses:
                assert re.search(r"@[0-9a-f]{40}$", uses), uses
