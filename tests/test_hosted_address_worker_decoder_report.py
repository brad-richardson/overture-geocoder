import json
import statistics
from pathlib import Path


ROOT = Path(__file__).parent.parent
REPORT = ROOT / "benchmarks" / "hosted-address-worker-decoder-report.json"
MARKDOWN = ROOT / "benchmarks" / "hosted-address-worker-decoder-report.md"


def test_worker_decoder_report_preserves_green_run_evidence():
    report = json.loads(REPORT.read_text())

    assert report["schema"] == "overture-address-worker-decoder-report-v1"
    assert report["verdict"] == "pass-bounded"
    assert report["fixture"]["candidate_count"] == 137
    assert "synthetic fixture" in report["fixture"]["origin"]
    assert "compact HTTP verification" in report["fixture"]["response_fidelity"]
    assert report["fixture"]["data_bytes"] == (
        report["fixture"]["page_offset"] + report["fixture"]["page_bytes"]
    )
    samples = report["samples"]
    assert [sample["attempt"] for sample in samples] == list(range(1, 7))
    assert samples[0]["sample_class"] == "first_run_unique_lookup"
    assert {sample["sample_class"] for sample in samples[1:]} == {"subsequent"}
    subsequent_wall = [sample["wall_milliseconds"] for sample in samples[1:]]
    subsequent_server = [sample["server_milliseconds"] for sample in samples[1:]]
    assert report["summary"]["first_run_unique_lookup_wall_milliseconds"] == samples[0]["wall_milliseconds"]
    assert report["summary"]["subsequent_median_wall_milliseconds"] == statistics.median(subsequent_wall)
    assert report["summary"]["subsequent_median_server_milliseconds"] == statistics.median(subsequent_server)
    assert report["summary"]["subsequent_min_wall_milliseconds"] == min(subsequent_wall)
    assert report["summary"]["subsequent_min_server_milliseconds"] == min(subsequent_server)
    assert report["cleanup"] == {"r2_prefix_empty": True, "worker_deleted": True}
    assert "/actions/runs/29445012372" in report["provenance"]["github_actions_run"]
    assert report["provenance"]["commit"] == "b390704fca0f0f57adee9140368f896ecfe5a148"
    assert report["provenance"]["github_actions_job"].endswith("/job/87453195064")


def test_worker_decoder_markdown_keeps_scope_limits_and_next_gate():
    markdown = MARKDOWN.read_text()

    assert "viable enough to continue" in markdown
    assert "does not prove global p95" in markdown
    assert "synthetic fixture" in markdown
    assert "did not measure the network" in markdown
    assert "Cache API hits were not" in markdown
    assert "representative large side index" in markdown
    assert "hash-verifying R2 shuffle/resume" in markdown
