"""Tests for the v2-aware retention guard.

The bug this closes is not "a missing check" but "absence read as safety": the
v1 guard reports *unreferenced* for anything reachable only through the v2
chain, including a live slice. So the tests are organised around the two ways
that failure can recur -- a reference the scanner does not look for, and a
document it could not read.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


GUARD = _load("v2_retention_guard", "scripts/v2_retention_guard.py")

LIVE_SLICE = "slice-2026-07-30.0"
ABANDONED_SLICE = "slice-2026-08-04.0"
LEGACY_CORE = "2026-07-18.0"


def _release(build: str, slice_version: str = LIVE_SLICE) -> dict:
    """A release document shaped like the real one, including the bucket-root
    keys the contract says entrypoints carry."""
    return {
        "schema": GUARD.RELEASE_SCHEMA,
        "geocoder_build": build,
        "overture_release": "2026-06-17.0",
        "legacy_core": {
            "version": LEGACY_CORE,
            "manifest_key": f"{LEGACY_CORE}/release-manifest.json",
            "entrypoints": {
                "forward": f"{LEGACY_CORE}/collection.json",
            },
        },
        "families": {
            "places": {
                "kind": "family_slice",
                "version": slice_version,
                "manifest_key": f"{slice_version}/slice-manifest.json",
                "entrypoints": {
                    "forward": f"{slice_version}/families/places/catalog.pcat",
                },
            }
        },
    }


def _write(path: Path, document: dict) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document, indent=2).encode()
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


@pytest.fixture
def chain(tmp_path):
    """A catalog with one release, written so its sha256 matches."""
    releases = tmp_path / "releases"
    digest = _write(releases / "2026-07-31.0" / "release.json", _release("2026-07-31.0"))
    catalog = tmp_path / "v2-catalog.json"
    catalog.write_text(
        json.dumps(
            {
                "schema": GUARD.CATALOG_SCHEMA,
                "generated_at": "2026-07-31T19:05:00Z",
                "latest": "2026-07-31.0",
                "releases": [
                    {
                        "geocoder_build": "2026-07-31.0",
                        "overture_release": "2026-06-17.0",
                        "manifest_key": "v2/releases/2026-07-31.0/release.json",
                        "manifest_sha256": digest,
                        "release_digest": "0" * 64,
                    }
                ],
                "catalog_digest": "1" * 64,
            }
        )
    )
    return catalog, releases


# --------------------------------------------------------------------------- #
# The defect itself
# --------------------------------------------------------------------------- #
def test_a_live_slice_is_reported_as_referenced(chain):
    """The exact case the v1 guard gets wrong."""
    catalog, releases = chain
    assert GUARD.main(
        ["assert-unreferenced", "--v2-catalog", str(catalog),
         "--releases-dir", str(releases), "--target", LIVE_SLICE]
    ) == 1


def test_an_abandoned_slice_is_deletable(chain):
    """...without making the guard useless: an unreferenced slice still passes."""
    catalog, releases = chain
    assert GUARD.main(
        ["assert-unreferenced", "--v2-catalog", str(catalog),
         "--releases-dir", str(releases), "--target", ABANDONED_SLICE]
    ) == 0


def test_the_legacy_core_version_is_also_protected(chain):
    """The chain protects plain versions too, not only slices."""
    catalog, releases = chain
    assert GUARD.main(
        ["assert-unreferenced", "--v2-catalog", str(catalog),
         "--releases-dir", str(releases), "--target", LEGACY_CORE]
    ) == 1


# --------------------------------------------------------------------------- #
# Failure mode 1: a reference the scanner was not told to look for
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "document",
    [
        pytest.param({"anything": "slice-2026-08-04.0"}, id="bare-value"),
        pytest.param(
            {"e": "slice-2026-08-04.0/families/places/head.phrp"}, id="entrypoint-key"
        ),
        pytest.param({"e": "./slice-2026-08-04.0/x.json"}, id="relative-href"),
        pytest.param({"slice-2026-08-04.0": {"bound": True}}, id="as-a-dict-key"),
        pytest.param(
            {"deep": [[{"a": [{"b": "slice-2026-08-04.0/x"}]}]]}, id="deeply-nested"
        ),
        pytest.param(
            {"future_field_nobody_designed": "slice-2026-08-04.0"}, id="unknown-field"
        ),
    ],
)
def test_the_scan_finds_a_reference_in_any_shape(document):
    """A recursive scan is the point: enumerating known fields is how the v1
    guard came to miss the v2 chain entirely."""
    assert ABANDONED_SLICE in GUARD.referenced_prefixes(document)


def test_a_partial_or_unrelated_string_is_not_a_reference():
    for value in (
        {"a": "slice-2026-08-04"},          # no ordinal
        {"a": "notslice-2026-08-04.0"},
        {"a": "staging/global-v2/abc/"},
        {"a": "families/places/head.phrp"},
    ):
        assert GUARD.referenced_prefixes(value) == set(), value


# --------------------------------------------------------------------------- #
# Failure mode 2: a document it could not read. Each MUST fail closed (exit 2),
# never report the target as safely deletable.
# --------------------------------------------------------------------------- #
def test_a_missing_release_document_fails_closed(chain, tmp_path):
    catalog, _ = chain
    empty = tmp_path / "empty"
    empty.mkdir()
    assert GUARD.main(
        ["assert-unreferenced", "--v2-catalog", str(catalog),
         "--releases-dir", str(empty), "--target", LIVE_SLICE]
    ) == 2


def test_a_release_document_with_the_wrong_bytes_fails_closed(chain):
    """A stale copy under-reports references, so it must not be trusted."""
    catalog, releases = chain
    path = releases / "2026-07-31.0" / "release.json"
    # Byte-changed but still valid, and still naming the live slice.
    path.write_text(json.dumps(_release("2026-07-31.0"), indent=4))
    assert GUARD.main(
        ["assert-unreferenced", "--v2-catalog", str(catalog),
         "--releases-dir", str(releases), "--target", ABANDONED_SLICE]
    ) == 2


def test_a_missing_catalog_fails_closed(tmp_path):
    assert GUARD.main(
        ["assert-unreferenced", "--v2-catalog", str(tmp_path / "nope.json"),
         "--releases-dir", str(tmp_path), "--target", ABANDONED_SLICE]
    ) == 2


def test_an_unrecognised_catalog_schema_fails_closed(tmp_path):
    catalog = tmp_path / "v2-catalog.json"
    catalog.write_text(json.dumps({"schema": "something-else", "releases": []}))
    assert GUARD.main(
        ["assert-unreferenced", "--v2-catalog", str(catalog),
         "--releases-dir", str(tmp_path), "--target", ABANDONED_SLICE]
    ) == 2


def test_a_partial_target_is_refused(chain):
    """Refuse to reason about a partial key rather than guess its prefix."""
    catalog, releases = chain
    for target in ("slice-2026-07-30.0/families", "families/places", "2026"):
        assert GUARD.main(
            ["assert-unreferenced", "--v2-catalog", str(catalog),
             "--releases-dir", str(releases), "--target", target]
        ) == 2, target


def test_an_unavailable_catalog_has_no_releases_and_protects_nothing(tmp_path):
    """A legitimate empty state -- distinct from an unreadable one."""
    catalog = tmp_path / "v2-catalog.json"
    catalog.write_text(json.dumps({"schema": GUARD.UNAVAILABLE_CATALOG_SCHEMA}))
    assert GUARD.main(
        ["assert-unreferenced", "--v2-catalog", str(catalog),
         "--releases-dir", str(tmp_path), "--target", ABANDONED_SLICE]
    ) == 0


def test_list_referenced_reports_every_prefix(chain, capsys):
    catalog, releases = chain
    assert GUARD.main(
        ["list-referenced", "--v2-catalog", str(catalog),
         "--releases-dir", str(releases)]
    ) == 0
    printed = set(capsys.readouterr().out.split())
    assert {LIVE_SLICE, LEGACY_CORE, "2026-07-31.0"}.issubset(printed)
    assert ABANDONED_SLICE not in printed


# --- workflow wiring -------------------------------------------------------
#
# The guard only protects what actually calls it, so the wiring is part of the
# contract: the failure being prevented -- deleting a live, serving slice --
# happens in the workflow, not in this module.
#
# These are deliberately STRUCTURAL rather than substring counts. A count says
# the guard is mentioned; it does not say the guard runs before the delete, in
# the same job, with its exit code still fatal. Each of `|| true` on the guard
# call, `continue-on-error: true` on its step, reordering the guard after the
# delete, and adding a fresh unguarded delete leaves a count-based test green
# while reopening the hole, so every one of those is asserted against below.

WORKFLOWS = Path(__file__).parent.parent / ".github/workflows"
CLEANUP = WORKFLOWS / "r2-cleanup.yml"
SLICE_FAMILIES = WORKFLOWS / "release-slice-families.yml"

GUARD_CALL = "scripts/v2_retention_guard.py assert-unreferenced"
# Every command in these workflows that can remove a whole prefix. `RM` is
# r2-cleanup's sourced helper; `aws s3 rm` is the raw form.
DELETES = ('RM "$target"', "aws s3 rm")
DEFUSED = ("|| true", "|| :", "|| exit 0")

# A delete needs the guard only if its target could BE a bucket root. These are
# the literal constraints by which r2-cleanup's phases 1, 2 and 4 pin their
# targets to a sub-prefix instead; each forces at least one interior `/`, which
# `is_bucket_root_prefix` rejects outright. Listing them here rather than
# skipping those phases is the point: a fourth unguarded delete that pins
# nothing fails this test instead of passing unnoticed.
SUB_PREFIX_PINS = (
    'case "$target" in */staging/) ;;',                     # phase 1
    "'^staging/global-v2/[0-9a-f]{64}/$'",                  # phases 2 and 4
    'staging/global-v2/*/"$ADDRESSES_SUBTREE") ;;',         # phase 4
)


def deleting_jobs(path):
    """(job name, ordered step list) for every job that deletes a prefix."""
    workflow = yaml.safe_load(path.read_text())
    out = []
    for name, job in workflow["jobs"].items():
        steps = job.get("steps", [])
        if any(any(d in s.get("run", "") for d in DELETES) for s in steps):
            out.append((name, steps))
    return out


@pytest.mark.parametrize("path", [CLEANUP, SLICE_FAMILIES])
def test_every_delete_is_guarded_or_pinned_below_the_bucket_root(path):
    """Zero-copy promotion publishes LIVE serving objects into
    `slice-YYYY-MM-DD.N/`, so any delete that can name a bucket root must ask the
    v2 chain first -- and must ask EARLIER in the job's step order than it
    deletes, not merely somewhere in the same file."""
    jobs = deleting_jobs(path)
    assert jobs, f"{path.name} has no delete job; update this test"
    for name, steps in jobs:
        runs = [step.get("run", "") for step in steps]
        guard_at = next((i for i, run in enumerate(runs) if GUARD_CALL in run), None)
        for index, run in enumerate(runs):
            if not any(delete in run for delete in DELETES):
                continue
            if any(pin in run for pin in SUB_PREFIX_PINS):
                continue
            assert guard_at is not None, (
                f"{path.name} job {name!r} step {steps[index].get('name')!r} "
                "deletes an unpinned prefix with no v2 guard; prune_catalog "
                "sees the v1 catalog only and calls a live slice unreferenced"
            )
            if guard_at == index:
                assert run.index(GUARD_CALL) < min(
                    run.index(delete) for delete in DELETES if delete in run
                ), f"{path.name} job {name!r} guards after it deletes"
            else:
                assert guard_at < index, (
                    f"{path.name} job {name!r} runs the guard in step "
                    f"{guard_at}, after the delete in step {index}"
                )


def test_the_sub_prefix_pins_really_do_exclude_a_bucket_root():
    """The exemption above is only sound because every pinned shape has an
    interior `/`. Asserted against the guard's own predicate so the two cannot
    drift apart."""
    for pinned in ("2026-07-17.0/staging/", "staging/global-v2/" + "a" * 64 + "/"):
        assert not GUARD.is_bucket_root_prefix(pinned.rstrip("/"))


@pytest.mark.parametrize("path", [CLEANUP, SLICE_FAMILIES])
def test_the_guards_verdict_stays_fatal(path):
    """A guard whose non-zero exit is swallowed is decoration. The chain fetch
    counts too: if it may fail softly, the guard runs against an empty or
    partial chain and reports a live slice as unreferenced."""
    for name, steps in deleting_jobs(path):
        for step in steps:
            run = step.get("run", "")
            if GUARD_CALL not in run and "v2-catalog.json" not in run:
                continue
            assert step.get("continue-on-error") is not True, (
                f"{path.name} job {name!r} step {step.get('name')!r} may fail "
                "softly, so the guard can run on a chain it never read"
            )
            assert "set -euo pipefail" in run
            # Join backslash continuations first: the guard invocation is a
            # multi-line command, so a `|| true` appended to its LAST line would
            # be invisible to a naive per-line scan while fully defusing it.
            for line in run.replace("\\\n", " ").splitlines():
                if GUARD_CALL in line or "v2/catalog.json" in line:
                    assert not any(token in line for token in DEFUSED), (
                        f"{path.name} job {name!r} defuses: {line.strip()}"
                    )


def test_the_guard_accepts_the_shape_zero_copy_promotion_publishes_into():
    """Zero-copy promotion writes serving objects into `slice-YYYY-MM-DD.N/`.
    An abandoned one has to stay removable, or a mistyped `release_slice_version`
    strands ~45 GiB (Places) or ~114 GiB (Addresses) permanently -- and phase 3
    emits a bare `<prefix>/` target with no version-format restriction, which is
    what makes a slice eligible in the first place."""
    workflow = CLEANUP.read_text()
    assert 'for version in $ORPHAN_PREFIXES; do' in workflow
    assert 'echo "3|$version/" >> /tmp/delete-targets.txt' in workflow
    assert GUARD.is_bucket_root_prefix("slice-2026-08-04.0")
    assert GUARD.is_bucket_root_prefix("2026-07-17.0")
    assert not GUARD.is_bucket_root_prefix("slice-2026-08-04.0/families")
