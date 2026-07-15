import json
import math
from pathlib import Path


ROOT = Path(__file__).parents[1]
REPORT = json.loads(
    (ROOT / "benchmarks" / "hosted-rowgroup-data-spike-report.json").read_text()
)
RAW = json.loads(
    (ROOT / "benchmarks" / "hosted-rowgroup-data-spike-raw.json").read_text()
)


def test_curated_report_preserves_primary_raw_measurements():
    assert REPORT["release"] == RAW["release"]
    assert REPORT["family"] == RAW["family"]
    assert REPORT["selection"]["rows"] == RAW["selection"]["rows"]
    assert REPORT["output"]["bytes"] == RAW["output"]["bytes"]
    assert REPORT["output"]["sha256"] == RAW["output"]["sha256"]
    assert REPORT["source"]["bytes"] == RAW["source"]["bytes"]
    assert (
        REPORT["resources"]["initial_network_received_bytes_upper_bound"]
        == RAW["resources"]["initial_network_received_bytes_upper_bound"]
    )


def test_global_diagnostics_recompute_from_checked_in_evidence():
    diagnostics = REPORT["diagnostics"]
    rows = REPORT["selection"]["rows"]
    planning_count = diagnostics["address_planning_count"]
    job_seconds = REPORT["run"]["job_duration_seconds"]
    covering_jobs = math.ceil(planning_count / rows)

    assert diagnostics["covering_jobs"] == covering_jobs == 335
    assert diagnostics["task_equivalents"] == planning_count / rows
    assert diagnostics["aggregate_runner_minutes_at_covering_jobs"] == (
        covering_jobs * job_seconds / 60
    )
    assert diagnostics["idealized_four_way_task_stage_minutes"] == (
        covering_jobs * job_seconds / 60 / 4
    )
    assert math.isclose(
        diagnostics["initial_receive_gb_decimal"],
        planning_count
        * REPORT["resources"]["initial_network_received_bytes_upper_bound"]
        / rows
        / 1e9,
    )
    assert math.isclose(
        diagnostics["output_gb_decimal"],
        planning_count * REPORT["output"]["bytes"] / rows / 1e9,
    )
