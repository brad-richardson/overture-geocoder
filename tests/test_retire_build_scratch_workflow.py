"""Structural contract tests for .github/workflows/retire-build-scratch.yml.

This workflow deletes build scratch, so its gates are the only thing standing
between a mistyped dispatch and a stranded planet build. These pin the gates
structurally rather than by wording.
"""

from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "retire-build-scratch.yml"


def load():
    return yaml.safe_load(WORKFLOW.read_text())


def job():
    return load()["jobs"]["retire"]


def body():
    return "\n".join(step.get("run") or "" for step in job()["steps"])


def test_families_is_read_as_object_keys_not_array_elements():
    # The slice manifest's `families` is an OBJECT keyed by family name. The
    # first real dispatch used `.families[]`, which yields the VALUES -- they
    # word-split to `{` and the run went looking for `claims/{.json`. Nothing
    # was deleted, because the claim gate failed closed on the bogus name, but
    # the workflow could never have completed.
    text = body()
    assert "jq -r '.families | keys[]'" in text
    assert "jq -r '.families[]'" not in text


def test_every_gate_precedes_the_delete():
    names = [step.get("name") for step in job()["steps"]]
    delete = names.index("Retire the scratch")
    for gate in (
        "Validate inputs (no secrets)",
        "Require the slice to be promoted",
        "Require the slice's own claims to name this request",
        "Require reverse to be published for every family",
        "Compute and guard the delete targets",
    ):
        assert names.index(gate) < delete, gate


def test_the_promotion_gate_requires_reference_not_absence():
    # Inverted on purpose: everywhere else the guard proves a prefix is
    # UNreferenced before deleting. Here an unpromoted slice's scratch is
    # exactly what promotion reads, so it must be REFERENCED.
    text = body()
    assert "v2_retention_guard.py list-referenced" in text
    assert 'grep -Fxq "$SLICE_VERSION" /tmp/referenced.txt' in text
    assert "assert-unreferenced" not in text


def test_targets_are_restricted_to_per_run_scratch_prefixes():
    text = body()
    assert r"^staging/global-v2/[0-9a-f]{64}/$" in text
    assert r"^construction-v1/[0-9a-f]{64}/$" in text


def test_dry_run_is_the_default():
    inputs = load()[True]["workflow_dispatch"]["inputs"]
    assert inputs["mode"]["default"] == "dry-run"
    assert inputs["mode"]["options"] == ["dry-run", "execute"]


def test_it_joins_the_v2_publication_concurrency_group():
    concurrency = load()["concurrency"]
    assert concurrency["group"] == "r2-v2-publication"
    assert concurrency["cancel-in-progress"] is False


def test_the_always_proof_tolerates_a_gate_refusing_first():
    # always() also runs when a gate refused before the manifest was fetched.
    # Without this the run reports `jq: no such file` instead of the real reason.
    assert "if [ ! -s /tmp/slice-manifest.json ]; then" in body()
