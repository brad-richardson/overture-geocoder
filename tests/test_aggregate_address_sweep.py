import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).parents[1]

_spec = importlib.util.spec_from_file_location(
    "aggregate_address_sweep", ROOT / "scripts" / "aggregate_address_sweep.py"
)
agg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(agg)


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


def _complete_task_evidence(
    evidence_dir: Path,
    name: str,
    *,
    input_rows: int,
    selected_rows: int,
    fragment_bytes: int,
    map_seconds: float,
    reduce_seconds: float,
    map_rss: int,
    reduce_rss: int,
    amplification: float,
    projected_rows: int,
    oracle_match: bool = True,
) -> None:
    # Mimic download-artifact's per-artifact subdirectory layout, with the
    # workflow's deep /tmp nesting for good measure, to exercise rglob lookup.
    sub = evidence_dir / f"address-resume-evidence-{name}-1-1"
    deep = sub / "tmp" / f"address-resume-{name}"
    _write(
        sub / f"map-{name}.json",
        {
            "map_fragments": {
                "input_rows": input_rows,
                "selected_rows": selected_rows,
                "bytes": fragment_bytes,
            },
            "resources": {
                "elapsed_seconds": map_seconds,
                "peak_rss_bytes": map_rss,
            },
        },
    )
    _write(
        sub / f"restored-reduce-{name}.json",
        {
            "resources": {
                "elapsed_seconds": reduce_seconds,
                "peak_rss_bytes": reduce_rss,
            },
            "local_oracle_match": oracle_match,
        },
    )
    _write(
        sub / f"resume-measurement-{name}.json",
        {
            "retry_readback_amplification": amplification,
            "fragment_bytes": fragment_bytes,
            "local_oracle_match": oracle_match,
        },
    )
    _write(
        sub / f"projection-{name}.json",
        {"selection": {"rows": projected_rows}},
    )
    # An unrelated generic file that collides across tasks; must be ignored.
    _write(deep / "full.json", {"artifacts": []})


def _selection(tasks: list[dict]) -> dict:
    return {"schema": "overture-address-sweep-selection-v1", "release": "r", "tasks": tasks}


def test_aggregates_completed_tasks_and_flags_missing(tmp_path):
    evidence = tmp_path / "sweep-evidence"
    _complete_task_evidence(
        evidence,
        "alpha",
        input_rows=1000,
        selected_rows=800,
        fragment_bytes=8000,
        map_seconds=100.0,
        reduce_seconds=90.0,
        map_rss=800,
        reduce_rss=700,
        amplification=3.0,
        projected_rows=1000,
    )
    _complete_task_evidence(
        evidence,
        "beta",
        input_rows=2000,
        selected_rows=1000,
        fragment_bytes=20000,
        map_seconds=200.0,
        reduce_seconds=150.0,
        map_rss=1600,
        reduce_rss=900,
        amplification=3.0,
        projected_rows=2000,
    )
    selection = _selection(
        [
            {"name": "alpha", "task_index": 1, "stratum": "s1", "expected_rows": 1000},
            {"name": "beta", "task_index": 2, "stratum": "s2", "expected_rows": 2000},
            {"name": "gamma", "task_index": 3, "stratum": "s3", "expected_rows": 3000},
        ]
    )

    summary = agg.aggregate(selection, evidence)

    assert summary["task_count"] == 3
    assert summary["completed_count"] == 2
    assert summary["incomplete_count"] == 1
    # gamma produced no evidence but the completed tasks still aggregate.
    incomplete = {t["name"] for t in summary["incomplete_tasks"]}
    assert incomplete == {"gamma"}
    assert summary["all_local_oracle_match"] is False  # gamma has no oracle result

    by_name = {t["name"]: t for t in summary["tasks"]}
    assert by_name["alpha"]["structured_retention_pct"] == 80.0
    assert by_name["alpha"]["output_bytes_per_retained_row"] == 10.0
    assert by_name["beta"]["structured_retention_pct"] == 50.0
    # peak RSS is the max of map and reduce.
    assert by_name["beta"]["peak_rss_bytes"] == 1600
    # Row reconciliation against the inventory expectation.
    assert by_name["alpha"]["rows_reconciled"] is True
    assert summary["rows_reconciled_count"] == 2

    dist = summary["distributions"]["structured_retention_pct"]
    assert dist["min"] == 50.0
    assert dist["max"] == 80.0
    assert dist["count"] == 2

    # Markdown renders without error and includes the table headers.
    md = agg.render_markdown(summary)
    assert "Per-task metrics" in md
    assert "Distribution across completed tasks" in md


def test_row_reconciliation_failure_is_flagged(tmp_path):
    evidence = tmp_path / "sweep-evidence"
    _complete_task_evidence(
        evidence,
        "alpha",
        input_rows=1000,
        selected_rows=800,
        fragment_bytes=8000,
        map_seconds=100.0,
        reduce_seconds=90.0,
        map_rss=800,
        reduce_rss=700,
        amplification=3.0,
        projected_rows=999,  # inventory expects 1000
    )
    selection = _selection(
        [{"name": "alpha", "task_index": 1, "stratum": "s1", "expected_rows": 1000}]
    )
    summary = agg.aggregate(selection, evidence)
    assert summary["rows_reconcile_failure_tasks"] == ["alpha"]
    assert summary["tasks"][0]["rows_delta"] == -1


def test_oracle_mismatch_is_reported(tmp_path):
    evidence = tmp_path / "sweep-evidence"
    _complete_task_evidence(
        evidence,
        "alpha",
        input_rows=1000,
        selected_rows=800,
        fragment_bytes=8000,
        map_seconds=100.0,
        reduce_seconds=90.0,
        map_rss=800,
        reduce_rss=700,
        amplification=3.0,
        projected_rows=1000,
        oracle_match=False,
    )
    selection = _selection(
        [{"name": "alpha", "task_index": 1, "stratum": "s1", "expected_rows": 1000}]
    )
    summary = agg.aggregate(selection, evidence)
    assert summary["oracle_mismatch_tasks"] == ["alpha"]
    assert summary["tasks"][0]["status"] == "oracle-mismatch"
    assert summary["all_local_oracle_match"] is False


def test_empty_evidence_still_produces_a_summary(tmp_path):
    evidence = tmp_path / "sweep-evidence"
    evidence.mkdir()
    selection = _selection(
        [{"name": "alpha", "task_index": 1, "stratum": "s1", "expected_rows": 1000}]
    )
    summary = agg.aggregate(selection, evidence)
    assert summary["completed_count"] == 0
    assert summary["distributions"]["structured_retention_pct"] is None
    # Rendering and CLI must not blow up on an all-missing run.
    md = agg.render_markdown(summary)
    assert "0/1" in md


def test_zero_retention_task_is_complete_not_missing(tmp_path):
    # A task that structures zero rows is a real 0% data point, not missing
    # evidence: it must stay "complete" and contribute to the retention
    # distribution, while bytes-per-retained-row is left undefined.
    evidence = tmp_path / "sweep-evidence"
    _complete_task_evidence(
        evidence,
        "alpha",
        input_rows=1000,
        selected_rows=0,
        fragment_bytes=0,
        map_seconds=100.0,
        reduce_seconds=90.0,
        map_rss=800,
        reduce_rss=700,
        amplification=3.0,
        projected_rows=1000,
    )
    selection = _selection(
        [{"name": "alpha", "task_index": 1, "stratum": "s1", "expected_rows": 1000}]
    )
    summary = agg.aggregate(selection, evidence)
    task = summary["tasks"][0]
    assert task["status"] == "complete"
    assert task["structured_retention_pct"] == 0.0
    assert task.get("output_bytes_per_retained_row") is None
    assert summary["completed_count"] == 1
    assert summary["distributions"]["structured_retention_pct"]["min"] == 0.0


def test_percentile_interpolates():
    assert agg._percentile([10, 20, 30, 40], 50) == 25.0
    assert agg._percentile([10], 95) == 10
    assert agg._percentile([1, 2, 3, 4, 5], 95) == 4.8


def test_cli_writes_json_and_markdown(tmp_path):
    evidence = tmp_path / "sweep-evidence"
    _complete_task_evidence(
        evidence,
        "alpha",
        input_rows=1000,
        selected_rows=800,
        fragment_bytes=8000,
        map_seconds=100.0,
        reduce_seconds=90.0,
        map_rss=800,
        reduce_rss=700,
        amplification=3.0,
        projected_rows=1000,
    )
    selection_path = tmp_path / "selection.json"
    selection_path.write_text(
        json.dumps(
            _selection(
                [
                    {
                        "name": "alpha",
                        "task_index": 1,
                        "stratum": "s1",
                        "expected_rows": 1000,
                    }
                ]
            )
        )
    )
    json_out = tmp_path / "out.json"
    md_out = tmp_path / "out.md"
    rc = agg.main(
        [
            "--selection",
            str(selection_path),
            "--evidence-dir",
            str(evidence),
            "--json-out",
            str(json_out),
            "--markdown-out",
            str(md_out),
        ]
    )
    assert rc == 0
    assert json.loads(json_out.read_text())["completed_count"] == 1
    assert md_out.read_text().startswith("# Stratified address sweep aggregate")
