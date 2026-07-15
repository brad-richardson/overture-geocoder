import json
from pathlib import Path


ROOT = Path(__file__).parent.parent
REPORT = ROOT / "benchmarks" / "hosted-address-compression-report.json"
MARKDOWN = ROOT / "benchmarks" / "hosted-address-compression-report.md"
WORKFLOW = ROOT / ".github" / "workflows" / "hosted-rowgroup-data-spike.yml"


def test_compression_evidence_reconciles_and_preserves_oracle():
    report = json.loads(REPORT.read_text())
    rows = report["input"]["records"]

    assert report["schema"] == "overture-address-compression-spike-summary-v1"
    assert report["input"]["bytes_per_indexed_row"] == round(report["input"]["bytes"] / rows, 6)
    assert report["oracle"] == {
        "candidate_order_and_ids_preserved": True,
        "distinct_lookup_keys": 1258445,
        "maximum_candidate_fanout": 137,
        "maximum_fanout_id_sha256": "cac637ef1ec7b632de30a3000f8d8b120a98e3480888c8e680e0e41c870cbb53",
    }
    for variant in report["variants"].values():
        assert variant["bytes_per_indexed_row"] == round(variant["total_bytes"] / rows, 6)


def test_compression_evidence_is_bound_to_ephemeral_public_run():
    report = json.loads(REPORT.read_text())
    provenance = report["provenance"]
    markdown = MARKDOWN.read_text()

    assert provenance["commit"] == "336e65ed0ddb0e708c8b23d0d525028cd2db8336"
    assert provenance["github_actions_run"].endswith("/29437656817")
    assert "not a global\nforecast" in markdown
    assert "No R2 object, artifact upload, catalog, or production state was written" in markdown


def test_hosted_workflow_runs_compression_without_credentials_or_uploads():
    workflow = WORKFLOW.read_text()

    assert "experiment_address_compression.py" in workflow
    assert "--page-rows 256" in workflow
    assert "persist-credentials: false" in workflow
    assert "permissions:\n  contents: read" in workflow
    assert "upload-artifact" not in workflow
