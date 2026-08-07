"""Structural contract tests for the two R2 retention workflows.

`r2-cleanup.yml` deletes bucket-root prefixes, and its last line of defence is a
STATIC `PROTECTED_PREFIXES` list. Static lists go stale: it was written when
2026-07-13.0 was the live latest, and by 2026-08-07 the latest was 2026-07-28.0
and absent from the list entirely. These tests cannot know which version is live
-- that needs the catalog -- but they can pin the invariants that hold
regardless, and catch the footgun where a protected prefix is also a phase
target, which would make every dispatch of that phase fail at runtime.

Note: PyYAML parses the ``on:`` trigger key as the boolean ``True`` (YAML 1.1).
"""

from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent
CLEANUP = ROOT / ".github" / "workflows" / "r2-cleanup.yml"
INVENTORY = ROOT / ".github" / "workflows" / "r2-inventory.yml"


def cleanup_env():
    return yaml.safe_load(CLEANUP.read_text())["env"]


def protected():
    return [p.strip("/") for p in cleanup_env()["PROTECTED_PREFIXES"].split() if p.strip("/")]


def intersects(target, prot):
    """The same either-direction rule the workflow's own guard applies."""
    return target == prot or target.startswith(prot + "/") or prot.startswith(target + "/")


# --- r2-cleanup protected prefixes -------------------------------------------


def test_protected_prefixes_is_non_empty_and_unique():
    values = protected()
    assert values, "PROTECTED_PREFIXES must never be empty"
    assert len(values) == len(set(values)), f"duplicate entries: {values}"


def test_backups_is_always_protected():
    # This workflow writes the catalog backups it would need to recover, so
    # deleting them is self-defeating regardless of which versions are live.
    assert "backups" in protected()


def test_no_static_phase_target_intersects_a_protected_prefix():
    # A version that is both protected and a phase target makes that phase fail
    # at dispatch, every time. Phase 1 in particular targets `<version>/staging/`
    # for versions it deliberately KEEPS, so protecting one of those versions at
    # the root would silently disable the phase.
    env = cleanup_env()
    targets = {
        "STAGING_ONLY": [f"{v}/staging" for v in env["STAGING_ONLY"].split()],
        "ORPHAN_PREFIXES": list(env["ORPHAN_PREFIXES"].split()),
        "PRUNE_VERSIONS": list(env["PRUNE_VERSIONS"].split()),
    }
    for name, values in targets.items():
        for target in values:
            for prot in protected():
                assert not intersects(target.strip("/"), prot), (
                    f"{name} target {target!r} intersects protected {prot!r}; "
                    "that phase would fail at every dispatch"
                )


def test_the_retention_guard_is_still_the_authority_for_prefix_deletes():
    # PROTECTED_PREFIXES is belt-and-braces. The phases that delete bucket-root
    # prefixes must consult the v2 chain, which is the only thing that can see a
    # live slice or a bound legacy core.
    body = "\n".join(
        step.get("run") or ""
        for step in yaml.safe_load(CLEANUP.read_text())["jobs"]["cleanup"]["steps"]
    )
    assert "v2_retention_guard.py assert-unreferenced" in body
    assert "prune_catalog.py assert-unreferenced" in body


# --- r2-inventory dispatch input ---------------------------------------------


def test_inventory_deep_prefixes_never_reaches_the_script_body():
    # A workflow_dispatch string interpolated into `run:` is shell injection into
    # a job holding R2 write credentials. It must arrive through the environment.
    step = yaml.safe_load(INVENTORY.read_text())["jobs"]["inventory"]["steps"][0]
    assert step["env"]["DEEP_PREFIXES"] == "${{ inputs.deep_prefixes }}"
    assert "inputs.deep_prefixes" not in step["run"]
    assert "$DEEP_PREFIXES" in step["run"]
