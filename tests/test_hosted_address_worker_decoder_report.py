import json
from pathlib import Path


ROOT = Path(__file__).parent.parent
REPORT = ROOT / "benchmarks" / "hosted-address-worker-decoder-report.json"
MARKDOWN = ROOT / "benchmarks" / "hosted-address-worker-decoder-report.md"


def test_worker_decoder_report_preserves_green_run_evidence():
    report = json.loads(REPORT.read_text())

    assert report["schema"] == "overture-address-worker-decoder-report-v1"
    assert report["verdict"] == "pass-bounded"
    assert report["fixture"]["candidate_count"] == 137
    assert report["summary"]["cold_wall_milliseconds"] == 434.450
    assert report["summary"]["warm_median_wall_milliseconds"] == 156.161
    assert len(report["samples"]) == 6
    assert report["cleanup"] == {"r2_prefix_empty": True, "worker_deleted": True}
    assert "/actions/runs/29445012372" in report["provenance"]["github_actions_run"]


def test_worker_decoder_markdown_keeps_scope_limits_and_next_gate():
    markdown = MARKDOWN.read_text()

    assert "viable enough to continue" in markdown
    assert "does not prove global p95" in markdown
    assert "representative large side index" in markdown
    assert "hash-verifying R2 shuffle/resume" in markdown
