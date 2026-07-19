import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import usa_scale_report as report  # noqa: E402


def _selection(task_count: int = 2) -> dict:
    tasks = [
        {
            "name": f"us-dominant-{index:03d}",
            "task_index": index,
            "expected_rows": 100 + index,
        }
        for index in range(task_count)
    ]
    return {
        "schema": report.SELECTION_SCHEMA,
        "release": "2026-06-17.0",
        "country": "US",
        "exact_country_export": False,
        "task_count": task_count,
        "projected_rows": sum(task["expected_rows"] for task in tasks),
        "tasks": tasks,
    }


def _addresses(selection: dict) -> dict:
    tasks = [
        {
            **task,
            "status": "complete",
            "input_rows": task["expected_rows"],
            "selected_rows": task["expected_rows"] - 1,
            "fragment_bytes": 1_000 + task["task_index"],
            "map_wall_seconds": 10.5,
            "reduce_wall_seconds": 5.25,
            "peak_rss_bytes": 900 + task["task_index"],
            "retry_read_amplification": 2.1,
            "local_oracle_match": True,
            "rows_reconciled": True,
        }
        for task in selection["tasks"]
    ]
    return {
        "schema": report.ADDRESS_SCHEMA,
        "release": selection["release"],
        "task_count": len(tasks),
        "completed_count": len(tasks),
        "rows_reconciled_count": len(tasks),
        "all_local_oracle_match": True,
        "selection_schema": report.SELECTION_SCHEMA,
        "selection_country": "US",
        "exact_country_export": False,
        "tasks": tasks,
    }


def _places(*, shard_bytes: int = 2_000) -> dict:
    digest = "a" * 64
    return {
        "schema": report.PLACES_SCHEMA,
        "release": "2026-06-17.0",
        "promotion_eligible": False,
        "extract": {
            "extracted_rows": 500,
            "extract_limit": 1_000,
            "truncated": False,
        },
        "determinism": {"determinism_ok": True},
        "totals": {
            "loaded_places": 500,
            "shard_rows": 500,
            "shards": 2,
            "shard_bytes": shard_bytes,
        },
        "family_manifest_digest": digest,
        "family_verification": {
            "family": "places",
            "manifest_digest": digest,
            "region": {
                "name": "conus",
                "bbox": [-125.0, 24.4, -66.9, 49.4],
                "bbox_scope": "exact",
            },
            "verified_objects": 4,
            "verified_bytes": shard_bytes + 100,
        },
    }


def test_combined_report_passes_and_preserves_non_exact_scope():
    selection = _selection()
    combined = report.build_report(_places(), _addresses(selection), selection)

    assert combined["schema"] == report.REPORT_SCHEMA
    assert combined["decision"] == "pass"
    assert combined["promotion_eligible"] is False
    assert combined["addresses"]["exact_country_export"] is False
    assert combined["addresses"]["tasks_completed"] == 2
    assert combined["addresses"]["expected_rows"] == 201
    assert combined["addresses"]["retained_rows"] == 199
    assert combined["addresses"]["fragment_bytes"] == 2_001
    assert combined["places"]["shard_bytes"] == 2_000
    assert combined["places"]["family_bytes"] == 2_100
    assert combined["gate"]["combined_measured_bytes"] == 4_101
    assert combined["gate"]["passed"] is True
    assert "not an exact" in report.render_markdown(combined)


@pytest.mark.parametrize(
    ("mutation", "result_key"),
    [
        (lambda document: document.update(completed_count=1), "complete"),
        (
            lambda document: document.update(all_local_oracle_match=False),
            "all_local_oracle_match",
        ),
        (
            lambda document: document.update(rows_reconciled_count=1),
            "all_rows_reconciled",
        ),
    ],
)
def test_combined_report_fails_closed_on_address_gate(mutation, result_key):
    selection = _selection()
    addresses = _addresses(selection)
    mutation(addresses)
    combined = report.build_report(_places(), addresses, selection)

    assert combined["decision"] == "fail"
    assert combined["gate"]["passed"] is False
    assert combined["addresses"][result_key] is False


def test_combined_report_fails_closed_above_byte_gate():
    selection = _selection()
    places = _places(shard_bytes=report.COMBINED_BYTE_GATE)
    combined = report.build_report(places, _addresses(selection), selection)
    assert combined["gate"]["within_combined_byte_limit"] is False
    assert combined["decision"] == "fail"


def test_rejects_task_identity_drift():
    selection = _selection()
    addresses = _addresses(selection)
    addresses["tasks"][0]["task_index"] = 99
    with pytest.raises(ValueError, match="exactly match"):
        report.build_report(_places(), addresses, selection)


def test_wrong_places_region_fails_the_manifest_gate():
    selection = _selection()
    places = _places()
    places["family_verification"]["region"]["name"] = "us-northeast"
    combined = report.build_report(places, _addresses(selection), selection)
    assert combined["places"]["manifest_verified"] is False
    assert combined["gate"]["passed"] is False


def test_cli_writes_evidence_and_returns_gate_status(tmp_path):
    selection = _selection()
    inputs = {
        "places": _places(),
        "addresses": _addresses(selection),
        "selection": selection,
    }
    paths = {}
    for name, document in inputs.items():
        paths[name] = tmp_path / f"{name}.json"
        paths[name].write_text(json.dumps(document))
    output = tmp_path / "report.json"
    markdown = tmp_path / "report.md"
    rc = report.main(
        [
            "--places",
            str(paths["places"]),
            "--addresses",
            str(paths["addresses"]),
            "--selection",
            str(paths["selection"]),
            "--output",
            str(output),
            "--markdown",
            str(markdown),
        ]
    )
    assert rc == 0
    assert json.loads(output.read_text())["decision"] == "pass"
    assert markdown.read_text().startswith("## USA scale signal")
