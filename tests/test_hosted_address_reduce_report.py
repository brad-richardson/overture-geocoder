import json
from pathlib import Path


ROOT = Path(__file__).parent.parent
REPORT = ROOT / "benchmarks" / "hosted-address-reduce-report.json"
MARKDOWN = ROOT / "benchmarks" / "hosted-address-reduce-report.md"


def test_reduce_report_reconciles_input_selection_fragments_and_artifact():
    report = json.loads(REPORT.read_text())
    input_rows = report["input"]["records"]
    selected = report["rejections"]["selected_records"]
    rejected = (
        report["rejections"]["missing_street_or_number"]
        + report["rejections"]["invalid_geometry"]
    )

    assert report["schema"] == "overture-address-reduce-spike-summary-v1"
    assert input_rows == selected + rejected
    assert report["map_fragments"]["records"] == selected
    assert report["artifact"]["records"] == selected
    assert report["rejections"]["selected_fraction"] == round(
        selected / input_rows, 9
    )
    assert report["artifact"]["bytes_per_indexed_row"] == round(
        report["artifact"]["bytes"] / selected, 6
    )


def test_reduce_evidence_is_bound_to_successful_ephemeral_run():
    report = json.loads(REPORT.read_text())
    provenance = report["provenance"]
    verification = report["verification"]
    markdown = MARKDOWN.read_text()

    assert provenance["commit"] == "c5b1ab01953d4466d48dd1dd32cef5e168b11e37"
    assert provenance["github_actions_run"].endswith("/29436633251")
    assert verification == {
        "exact_candidate_sets": 3,
        "full_sorted_scan": True,
        "highest_fanout_candidate_count": 137,
        "highest_fanout_id_digest_match": True,
        "record_count_match": True,
    }
    assert "R2 fragment upload/download remains unmeasured" in markdown
    assert "No artifact was uploaded or published" in markdown


def test_reduce_report_does_not_overstate_one_range_as_global_forecast():
    markdown = MARKDOWN.read_text()

    assert "not a forecast" in markdown
    assert "not a representative" in markdown
    assert "global completeness measurement" in markdown
    assert "63.07%" in markdown
    assert "severe coverage" in markdown
    assert "70.0 GB" in markdown
