import copy
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).parent.parent


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fixtures = load_module(
    "v2_release_test_fixtures", ROOT / "tests" / "test_v2_release_manifest.py"
)
overlay = load_module(
    "prepare_v2_preview_overlay",
    ROOT / "scripts" / "prepare_v2_preview_overlay.py",
)
acceptance = load_module(
    "validate_v2_preview_results",
    ROOT / "scripts" / "validate_v2_preview_results.py",
)


def test_overlay_replaces_places_and_retains_addresses_on_its_source():
    base = fixtures.release_manifest("2026-08-02.0")
    legacy = fixtures.legacy_release()
    base_source = fixtures.family_source_manifest("places", "addresses")
    address_manifest = fixtures.family_manifest("addresses")
    candidate_manifest = fixtures.gbm.build_family_manifest(
        "places",
        lineage={
            "overture_release": fixtures.RELEASE,
            "build_id": "a" * 64,
            "producer_commit": "deadbeef",
            "producer_script": "scripts/places_construction_v1.py",
            "producer_version": "1",
        },
        versions={
            "format": "PLRV0003+PLHD0003",
            "tokenizer": "nfkd-lower-stripmark-cjk-bigram-v4",
            "normalization": None,
        },
        region={
            "name": "global",
            "bbox": [-180.0, -90.0, 180.0, 90.0],
            "bbox_scope": "exact",
        },
        artifacts=[
            {
                "object_key": "families/places/routing.json",
                "bytes": 123,
                "sha256": "c" * 64,
            }
        ],
        generated_at="2026-08-03T00:00:00+00:00",
    )
    candidate_source = fixtures.family_source_manifest(
        "places", version="slice-2026-08-03.0"
    )
    candidate_source["families"]["places"] = {
        "manifest": "./families/places/family-manifest.json",
        "manifest_digest": candidate_manifest["manifest_digest"],
        "region": candidate_manifest["region"],
        "artifact_count": 1,
        "total_bytes": 123,
        "objects": [
            {
                "href": "./families/places/routing.json",
                "size_bytes": 123,
                "sha256": "c" * 64,
            }
        ],
        "promotion_eligible": False,
    }
    candidate_source["verified_version_objects"] = [
        {"href": "./families/places/family-manifest.json"},
        {
            "href": "./families/places/routing.json",
            "size_bytes": 123,
            "sha256": "c" * 64,
        },
    ]

    release, report = overlay.build_overlay_release(
        base_release=base,
        legacy_release=legacy,
        legacy_sha256=fixtures.payload_sha(legacy),
        base_family_manifests={
            "addresses": (address_manifest, fixtures.payload_sha(address_manifest))
        },
        base_family_sources={
            "addresses": (base_source, fixtures.payload_sha(base_source))
        },
        candidate_family="places",
        candidate_manifest=candidate_manifest,
        candidate_manifest_sha256=fixtures.payload_sha(candidate_manifest),
        candidate_source=candidate_source,
        candidate_source_sha256=fixtures.payload_sha(candidate_source),
        geocoder_build="2026-08-03.0",
    )

    assert release["geocoder_build"] == "2026-08-03.0"
    assert release["families"]["places"]["source"]["version"] == (
        "slice-2026-08-03.0"
    )
    assert release["families"]["addresses"]["source"]["version"] == (
        fixtures.SLICE
    )
    assert release["families"]["places"]["operations"] == ["forward"]
    assert release["families"]["addresses"]["operations"] == [
        "structured_forward"
    ]
    assert report["base_build"] == "2026-08-02.0"
    assert report["retained_families"] == ["addresses"]


def benchmark_payload(*, found_at_1=2, found_at_10=2, losses=0):
    rows = [
        {
            "case_id": "gold:name:big-ben",
            "provider": "overture",
            "status": 200,
            "capability": "supported",
            "error": None,
            "found_at_10": True,
        },
        {
            "case_id": "gold:name:empire-state-building",
            "provider": "overture",
            "status": 200,
            "capability": "supported",
            "error": None,
            "found_at_10": True,
        },
    ]
    return {
        "meta": {"data_version": "2026-08-03.0"},
        "results": rows,
        "summary": {
            "overall": {
                "found_at_1": found_at_1,
                "found_at_10": found_at_10,
                "recall_at_1": found_at_1 / 2,
                "recall_at_10": found_at_10 / 2,
            }
        },
        "paired_comparison": {
            "groups": {
                "overall": {
                    "found_at_1": {"flips_to_hit": 0, "flips_to_miss": losses},
                    "found_at_10": {"flips_to_hit": 0, "flips_to_miss": losses},
                }
            }
        },
    }


def test_preview_acceptance_requires_zero_paired_losses_and_phrase_hits():
    preview = benchmark_payload()
    baseline = copy.deepcopy(preview)
    result = acceptance.validate_one(
        label="gold",
        preview=preview,
        baseline=baseline,
        expected_build="2026-08-03.0",
        required_at_10={
            "gold:name:big-ben",
            "gold:name:empire-state-building",
        },
    )
    assert result["preview"]["recall_at_10"] == 1.0

    regressed = benchmark_payload(losses=1)
    with pytest.raises(ValueError, match="paired found_at_1 regression"):
        acceptance.validate_one(
            label="gold",
            preview=regressed,
            baseline=baseline,
            expected_build="2026-08-03.0",
            required_at_10=set(),
        )


def test_preview_workflow_is_run_scoped_and_cleans_only_its_prefix():
    workflow = (
        ROOT / ".github" / "workflows" / "preview-v2-candidate.yml"
    ).read_text()
    assert "smoketest-v2/${{ github.run_id }}-${{ github.run_attempt }}" in workflow
    assert 'aws s3 rm "s3://${R2_BUCKET}/${PREVIEW_PREFIX}/" --recursive' in workflow
    assert "v2/catalog.json preview/input/production-catalog.json" in workflow
    assert "--expect-absent" in workflow
    assert "wrangler.global-v2-preview.toml" in workflow
    assert "validate_v2_preview_results.py" in workflow
    assert "s3://geocoder-shards/ --recursive" not in workflow


def test_preview_scripts_parse_committed_benchmarks(tmp_path):
    gold = json.loads(
        (ROOT / "benchmarks" / "2026-08-03-forward-gold-external-after-a2bc.json")
        .read_text()
    )
    everyday = json.loads(
        (ROOT / "benchmarks" / "2026-08-03-everyday-poi-external-baseline-v1.json")
        .read_text()
    )
    assert len(acceptance.provider_rows(gold)) == 55
    assert len(acceptance.provider_rows(everyday)) == 200
