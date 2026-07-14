import json
import subprocess
from pathlib import Path


WORKFLOW = (
    Path(__file__).parent.parent / ".github" / "workflows" / "rebuild-r2-shards.yml"
)
CLEANUP_WORKFLOW = (
    Path(__file__).parent.parent / ".github" / "workflows" / "r2-cleanup.yml"
)
PATCH_ID_WORKFLOW = (
    Path(__file__).parent.parent / ".github" / "workflows" / "patch-id-stage.yml"
)


def _text():
    return WORKFLOW.read_text()


def test_scheduled_rebuild_requires_explicit_repository_guard():
    text = _text()
    assert "vars.ENABLE_SCHEDULED_REBUILD == 'true'" in text
    assert "default: false" in text[text.index("cleanup_old_versions:"):]


def test_promotion_requires_one_complete_release_finalizer():
    text = _text()
    assert "finalize-release:" in text
    assert "needs: [prep, rebuild-shards, id-post]" in text
    assert "needs.prep.outputs.complete == 'true'" in text
    assert "needs.prep.outputs.promote == 'true'" in text
    assert "Promotion requires a complete build" in text
    assert "Publish catalog.json" not in text


def test_finalizer_verifies_manifest_before_catalog_and_smoke_before_retention():
    text = _text()
    verify = text.index("Read back and verify the exact complete release")
    manifest = text.index("Publish immutable release manifest")
    promote = text.index("Atomically promote and smoke production")
    prune = text.index("Prune only after successful promotion and smoke")
    assert verify < manifest < promote < prune
    assert "scripts/finalize_rebuild.py verify" in text
    assert "scripts/finalize_rebuild.py catalog" in text
    assert "release-manifest.json" in text
    for endpoint in ("/health", "/search", "/reverse", "/id/"):
        assert endpoint in text[promote:prune]
    assert "KEEP_VERSIONS=3" in text[prune:]


def test_existing_version_prefix_is_rejected_before_build_jobs():
    text = _text()
    collision = text.index("Version prefix ${VERSION}/ already exists in R2")
    rebuild_job = text.index("\n  rebuild-shards:")
    assert collision < rebuild_job


def test_release_types_use_seven_dedicated_runners_and_verified_barrier():
    text = _text()
    release_job = text[text.index("\n  id-stage-release:"):
                       text.index("\n  id-stage-release-finalize:")]
    assert "max-parallel: 7" in release_job
    assert "timeout-minutes: 180" in release_job
    for release_type in (
        "addresses/address",
        "base/bathymetry",
        "base/infrastructure",
        "base/land",
        "base/land_cover",
        "base/land_use",
        "base/water",
    ):
        assert f"- {release_type}" in release_job
    assert '--release-type "${{ matrix.release_type }}"' in release_job

    barrier = text[text.index("\n  id-stage-release-finalize:"):
                   text.index("\n  id-dictionary:")]
    assert "needs: [prep, id-stage-release]" in barrier
    assert "--phase stage-base-finalize" in barrier
    assert "needs: [prep, id-stage-registry, id-stage-release-finalize]" in text


def test_opaque_id_phases_use_application_heartbeats_and_batched_schema_scan():
    script = (
        Path(__file__).parent.parent / "scripts" / "build_id_index.py"
    ).read_text()
    assert "HEARTBEAT_INTERVAL_S = 5 * 60" in script
    assert "RELEASE_STAGE_THREADS = 8" in script
    assert 'RELEASE_STAGE_MEMORY = "10GB"' in script
    assert "download, range-filter, sort, and merge release staging" in script
    assert "schema batch {batch_index}/{total_batches}" in script


def test_all_production_catalog_writers_share_one_concurrency_lock():
    assert "group: r2-production-catalog" in _text()
    assert "group: r2-production-catalog" in CLEANUP_WORKFLOW.read_text()
    patch_text = PATCH_ID_WORKFLOW.read_text()
    assert "group: r2-production-catalog" in patch_text
    assert "release-manifest.json" in patch_text


def test_promotion_rollback_is_guarded_and_confirms_cached_previous_version():
    text = _text()
    assert "trap rollback_on_exit EXIT" in text
    assert "catalog-next.json" in text[text.index("rollback_candidate()") :]
    assert 'smoke_once "$PREVIOUS_VERSION"' in text
    assert "backups/catalog-before-${VERSION}.json" in text
    assert "post-finalize:" in text
    assert "catalog-candidate-${VERSION}.json" in text


def test_id_patch_rejects_catalogued_versions_and_rebuilds_complete_ranges():
    text = PATCH_ID_WORKFLOW.read_text()
    assert "PATCH_VERSION" in text and "patch-catalog.json" in text
    assert "catalogued and immutable" in text
    assert "any(.links[]?;" in text
    assert "Force complete rebuilds of affected ranges" in text
    assert "build-patched:" not in text

    catalog = {
        "links": [
            {"rel": "child", "href": "./2026-07-13.0/collection.json"},
            {"rel": "child", "href": "./2026-06-01.0/collection.json"},
        ]
    }
    result = subprocess.run(
        [
            "jq", "-e", "--arg", "version", "2026-07-13.0",
            'any(.links[]?; .rel == "child" and '
            '((.href | ltrimstr("./") | split("/")[0]) == $version))',
        ],
        input=json.dumps(catalog),
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0
