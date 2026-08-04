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


# ---------------------------------------------------------------------------
# Failure classification. Six v4 preview attempts failed for three different
# reasons and all surfaced identically; the class is what makes the retry
# decision mechanical.


def reject(preview, baseline=None, *, expected_build="2026-08-03.0",
           required_at_10=frozenset()):
    with pytest.raises(acceptance.Rejection) as caught:
        acceptance.validate_one(
            label="gold",
            preview=preview,
            baseline=copy.deepcopy(preview) if baseline is None else baseline,
            expected_build=expected_build,
            required_at_10=set(required_at_10),
        )
    return caught.value


def test_failed_requests_classify_as_operational_transient():
    """Attempt 5 (55x404) and attempt 6 (1 timeout + 2x500) were retryable."""
    preview = benchmark_payload()
    baseline = copy.deepcopy(preview)
    preview["results"][0]["status"] = 500
    rejection = reject(preview, baseline)
    assert rejection.failure_class == acceptance.OPERATIONAL_TRANSIENT
    assert rejection.detail["failed"] == 1
    assert rejection.detail["of"] == 2
    assert rejection.detail["status_histogram"] == {"500": 1}
    assert rejection.detail["all_requests_failed"] is False
    assert "Retry" in acceptance.RETRY_ADVICE[rejection.failure_class]


def test_every_request_failing_is_still_transient_but_says_so():
    preview = benchmark_payload()
    baseline = copy.deepcopy(preview)
    for row in preview["results"]:
        row["status"] = 404
    rejection = reject(preview, baseline)
    assert rejection.failure_class == acceptance.OPERATIONAL_TRANSIENT
    assert rejection.detail["all_requests_failed"] is True


def test_a_paired_loss_classifies_as_quality_regression_and_forbids_retry():
    baseline = benchmark_payload()
    rejection = reject(benchmark_payload(losses=1), baseline)
    assert rejection.failure_class == acceptance.QUALITY_REGRESSION
    assert "DO NOT retry" in acceptance.RETRY_ADVICE[rejection.failure_class]


def test_an_aggregate_regression_classifies_as_quality_regression():
    baseline = benchmark_payload(found_at_1=2, found_at_10=2)
    rejection = reject(benchmark_payload(found_at_1=1, found_at_10=2), baseline)
    assert rejection.failure_class == acceptance.QUALITY_REGRESSION
    assert rejection.detail["baseline"] == 2
    assert rejection.detail["preview"] == 1


def test_a_missing_required_case_classifies_as_quality_regression():
    preview = benchmark_payload()
    baseline = copy.deepcopy(preview)
    preview["results"][0]["found_at_10"] = False
    rejection = reject(
        preview, baseline, required_at_10={"gold:name:big-ben"}
    )
    assert rejection.failure_class == acceptance.QUALITY_REGRESSION


def test_a_wrong_data_version_classifies_as_setup():
    preview = benchmark_payload()
    baseline = copy.deepcopy(preview)
    preview["meta"]["data_version"] = "2026-08-02.0"
    rejection = reject(preview, baseline)
    assert rejection.failure_class == acceptance.SETUP
    assert rejection.detail == {
        "expected": "2026-08-03.0",
        "served": "2026-08-02.0",
    }


def test_a_case_set_mismatch_classifies_as_setup():
    preview = benchmark_payload()
    baseline = copy.deepcopy(preview)
    baseline["results"][0]["case_id"] = "gold:name:something-else"
    rejection = reject(preview, baseline)
    assert rejection.failure_class == acceptance.SETUP


def test_an_unreadable_document_classifies_as_setup(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not json")
    with pytest.raises(acceptance.Rejection) as caught:
        acceptance.read_document(path, "gold preview")
    assert caught.value.failure_class == acceptance.SETUP


def _write(path, payload):
    path.write_text(json.dumps(payload))
    return path


def test_the_cli_writes_a_classified_rejection_instead_of_a_traceback(tmp_path):
    """Previously a rejection escaped as a traceback and no acceptance document
    existed at all, so the run summary had nothing to report."""
    preview = benchmark_payload()
    baseline = copy.deepcopy(preview)
    preview["results"][0]["error"] = "http 500"
    output = tmp_path / "acceptance.json"
    code = acceptance.main([
        "--expected-build", "2026-08-03.0",
        "--gold-preview", str(_write(tmp_path / "gp.json", preview)),
        "--gold-baseline", str(_write(tmp_path / "gb.json", baseline)),
        "--everyday-preview", str(_write(tmp_path / "ep.json", preview)),
        "--everyday-baseline", str(_write(tmp_path / "eb.json", baseline)),
        "--output", str(output),
    ])
    assert code == 1
    written = json.loads(output.read_text())
    assert written["accepted"] is False
    assert written["failure_class"] == acceptance.OPERATIONAL_TRANSIENT
    assert written["retry_advice"]
    assert written["detail"]["failed"] == 1


def test_the_cli_still_accepts_a_clean_preview_and_reports_retries(tmp_path):
    preview = benchmark_payload()
    preview["meta"]["transient_retries_allowed"] = 2
    preview["meta"]["transient_retries_used"] = 3
    preview["meta"]["cases_with_transient_retry"] = 2
    baseline = copy.deepcopy(preview)
    output = tmp_path / "acceptance.json"
    code = acceptance.main([
        "--expected-build", "2026-08-03.0",
        "--gold-preview", str(_write(tmp_path / "gp.json", preview)),
        "--gold-baseline", str(_write(tmp_path / "gb.json", baseline)),
        "--everyday-preview", str(_write(tmp_path / "ep.json", preview)),
        "--everyday-baseline", str(_write(tmp_path / "eb.json", baseline)),
        "--output", str(output),
    ])
    assert code == 0
    written = json.loads(output.read_text())
    assert written["accepted"] is True
    # A run that needed retries to look clean must say so on the accept path.
    assert written["gold"]["retries"]["transient_retries_used"] == 3
    assert written["gold"]["retries"]["cases_with_transient_retry"] == 2


def test_the_workflow_classifies_its_own_failure():
    workflow = (
        ROOT / ".github" / "workflows" / "preview-v2-candidate.yml"
    ).read_text()
    assert "name: Classify the failure" in workflow
    assert "if: failure()" in workflow
    # The acceptance document must be preferred over step outcomes.
    assert ".failure_class // empty" in workflow
    for step_id in (
        "setup_python",
        "setup_worker_toolchain",
        "bind_release",
        "publish_preview",
        "deploy_preview",
        "warmup",
        "gold_benchmark",
        "everyday_benchmark",
        "acceptance",
    ):
        assert f"id: {step_id}" in workflow
        assert f"steps.{step_id}.outcome" in workflow or step_id == "acceptance"
    for label in ("setup", "operational-transient", "unclassified"):
        assert label in workflow


def test_preview_workflow_is_run_scoped_and_cleans_only_its_prefix():
    workflow = (
        ROOT / ".github" / "workflows" / "preview-v2-candidate.yml"
    ).read_text()
    benchmark_requirements = (
        ROOT / ".github" / "requirements-preview-v2.txt"
    ).read_text()
    assert "smoketest-v2/${{ github.run_id }}-${{ github.run_attempt }}" in workflow
    assert 'aws s3 rm "s3://${R2_BUCKET}/${PREVIEW_PREFIX}/" --recursive' in workflow
    assert "v2/catalog.json preview/input/production-catalog.json" in workflow
    assert "--expect-absent" in workflow
    assert "wrangler.global-v2-preview.toml" in workflow
    assert "validate_v2_preview_results.py" in workflow
    assert "cargo install worker-build --version '^0.7' --locked" in workflow
    assert "-r .github/requirements-preview-v2.txt" in workflow
    for pin in (
        "requests==2.34.2",
        "certifi==2026.7.22",
        "charset-normalizer==3.4.9",
        "idna==3.18",
        "urllib3==2.7.0",
    ):
        assert pin in benchmark_requirements
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
