from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "build-places-region.yml"


def test_region_build_is_manual_main_only_and_isolated():
    workflow = WORKFLOW.read_text()
    trigger = workflow[workflow.index("on:") : workflow.index("permissions:")]

    # Manual, credentialed, main-only (PENDING_WORK.md decisions 6 and 8).
    assert "workflow_dispatch:" in trigger
    assert "push:" not in trigger
    assert "pull_request:" not in trigger
    assert "schedule:" not in trigger
    assert "contents: read" in workflow
    assert "if: github.ref == 'refs/heads/main'" in workflow
    assert "persist-credentials: false" in workflow
    # A concurrency group that never cancels an in-flight credentialed upload.
    assert "group: build-places-region" in workflow
    assert "cancel-in-progress: false" in workflow

    # Every object lives under the run-unique, isolated, non-promoting prefix.
    assert "REGION_PREFIX: smoke/places-region/${{ github.run_id }}-${{ github.run_attempt }}" in workflow
    assert "s3://geocoder-shards/${REGION_PREFIX}/" in workflow


def test_region_build_never_touches_production_prefixes_or_catalog():
    workflow = WORKFLOW.read_text()

    # No production catalog swap, no release layout, no promotion.
    assert "catalog.json" not in workflow
    assert "releases/" not in workflow
    assert "promote" not in workflow
    # The only R2 prefix written or deleted is the isolated smoke prefix.
    assert workflow.count("s3://geocoder-shards/") == 1
    assert "smoke/places-region/" in workflow


def test_region_build_asserts_no_truncation_and_determinism():
    workflow = WORKFLOW.read_text()

    # Full extraction: a count at the extract limit is truncation and fails.
    assert "experiment_places_partition_extract.py" in workflow
    assert 'if [ "$ROWS" -ge "$EXTRACT_LIMIT" ]; then' in workflow
    # Build twice and byte-compare every produced object.
    assert workflow.count("build_places_region_shards.py") >= 2
    assert 'cmp "/tmp/region/a/${OBJ}" "/tmp/region/b/${OBJ}"' in workflow
    assert "determinism_ok:true" in workflow


def test_region_build_uploads_verified_and_wires_the_family_manifest():
    workflow = WORKFLOW.read_text()

    # Hash-verifying store: verified upload readback and verified restore.
    assert "scripts/r2_verified_store.py" in workflow
    assert "upload-manifest" in workflow
    assert "restore-manifest" in workflow
    assert "readback_verified == true" in workflow
    assert ".verified == true" in workflow

    # #107 family manifest: build (family places, exact scope) and verify.
    assert "global_build_manifest.py family-manifest" in workflow
    assert "--family places" in workflow
    assert "--bbox-scope exact" in workflow
    assert "global_build_manifest.py verify-family-manifest" in workflow

    # Evidence is always retained; the isolated prefix is cleaned up on request.
    assert "overture-places-region-build-evidence-v1" in workflow
    assert "actions/upload-artifact@v7" in workflow
    assert "if: always()" in workflow
    assert "if: ${{ always() && inputs.cleanup }}" in workflow
    assert 'test "$REMAINING" = "0"' in workflow
