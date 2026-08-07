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
# The guard only protects what actually calls it. These assert the wiring in
# `r2-cleanup.yml`, because the failure being prevented -- deleting a live,
# serving slice -- happens in the workflow, not in this module.

CLEANUP = Path(__file__).parent.parent / ".github/workflows/r2-cleanup.yml"


def test_every_bucket_root_delete_phase_consults_the_v2_guard():
    """Phases 3 and 5 are the two that delete a whole bucket-root prefix, and
    both must check the v2 chain -- `prune_catalog assert-unreferenced` sees
    the v1 catalog only and reports a live slice as unreferenced."""
    workflow = CLEANUP.read_text()
    calls = workflow.count("scripts/v2_retention_guard.py assert-unreferenced")
    assert calls == 2, (
        "expected the v2 guard in exactly the two bucket-root delete phases "
        f"(3 and 5); found {calls}"
    )
    # It must be given the whole chain, not just the catalog.
    # >= 2 rather than == 2: a pre-flight `list-referenced` step also passes
    # the chain, and pinning that count would break on an added diagnostic.
    assert workflow.count("--releases-dir /tmp/v2-releases") >= 2
    # And the chain must actually be fetched before any delete.
    assert 's3 cp "s3://$BUCKET/v2/catalog.json" /tmp/v2-catalog.json' in workflow


def test_phase_three_can_target_a_slice_prefix():
    """Zero-copy promotion writes serving objects into `slice-YYYY-MM-DD.N/`.
    An abandoned one has to be removable, or a mistyped `release_slice_version`
    strands ~45 GiB (Places) or ~114 GiB (Addresses) permanently. Phase 3 emits
    a bare `<prefix>/` target with no version-format restriction, which is what
    makes a slice eligible."""
    workflow = CLEANUP.read_text()
    assert 'for version in $ORPHAN_PREFIXES; do' in workflow
    assert 'echo "3|$version/" >> /tmp/delete-targets.txt' in workflow
    # The guard itself must accept the slice shape.
    assert GUARD.is_bucket_root_prefix("slice-2026-08-04.0")
    assert GUARD.is_bucket_root_prefix("2026-07-17.0")
    assert not GUARD.is_bucket_root_prefix("slice-2026-08-04.0/families")
