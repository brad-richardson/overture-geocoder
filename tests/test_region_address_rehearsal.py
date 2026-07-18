from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


region = _load("region_address_rehearsal")
gbm = _load("global_build_manifest")


def scoped_report(task_rows: list[int], *, bbox_scope="row_group_approximate", release="2026-06-17.0"):
    tasks = [
        {
            "index": index,
            "rows": rows,
            "selected_compressed_bytes": rows,
            "selected_uncompressed_bytes": rows * 2,
        }
        for index, rows in enumerate(task_rows)
    ]
    plan = {
        "schema": "overture-address-rowgroup-plan-v1",
        "task_count": len(tasks),
        "tasks": tasks,
        "task_rows": {"min": 0, "p50": 0, "p95": 0, "max": 0, "mean": 0},
        "task_selected_uncompressed_bytes": {"min": 0, "p50": 0, "p95": 0, "max": 0, "mean": 0},
    }
    if bbox_scope is not None:
        plan["bbox_scope"] = bbox_scope
        plan["bbox"] = {"xmin": -80.5, "ymin": 38.0, "xmax": -66.9, "ymax": 47.5}
        plan["bbox_row_groups"] = {
            "total": 100,
            "selected": len(tasks),
            "pruned": 100 - len(tasks),
            "no_stats_conservative": 0,
        }
        plan["bbox_scoped_rows"] = sum(task_rows)
    return {"schema": "overture-address-rowgroup-inventory-v1", "release": release, "plan": plan}


def write(tmp_path: Path, name: str, value) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(value))
    return path


# --- matrix / cap ---------------------------------------------------------


def test_matrix_includes_every_task_in_order():
    report = scoped_report([10, 20, 30])
    matrix = region.build_region_matrix(report, max_tasks=40)
    assert matrix == {
        "include": [
            {"name": "region-000", "task_index": 0},
            {"name": "region-001", "task_index": 1},
            {"name": "region-002", "task_index": 2},
        ]
    }


def test_matrix_fails_when_plan_exceeds_cap():
    report = scoped_report([1, 1, 1, 1, 1])
    with pytest.raises(region.RegionRehearsalError, match="exceeding the hard cap"):
        region.build_region_matrix(report, max_tasks=4)


def test_matrix_rejects_empty_scope():
    with pytest.raises(region.RegionRehearsalError, match="empty"):
        region.build_region_matrix(scoped_report([]), max_tasks=40)


def test_load_rejects_non_region_plan(tmp_path):
    # A default (non-bbox) inventory report is refused at load time.
    plain = write(tmp_path, "plain.json", scoped_report([10], bbox_scope=None))
    with pytest.raises(region.RegionRehearsalError, match="bbox-scoped plan"):
        region.load_scoped_report(plain)


def test_plan_summary_carries_scope_and_cap():
    report = scoped_report([10, 20])
    summary = region.scoped_plan_summary(report, region_name="us-northeast", max_tasks=40)
    assert summary["region"]["name"] == "us-northeast"
    assert summary["region"]["bbox_scope"] == "row_group_approximate"
    assert summary["max_tasks"] == 40
    assert summary["bbox_scoped_rows"] == 30
    assert summary["task_count"] == 2


# --- reconciliation -------------------------------------------------------


def map_report(rows: int):
    return {
        "schema": "overture-address-verified-resume-map-v1",
        "map_fragments": {"input_rows": rows},
    }


def test_reconcile_exact_match():
    report = scoped_report([10, 20, 30])
    entries = [
        region.task_rows_entry(map_report(rows), task_index=index, name=f"region-{index:03d}")
        for index, rows in enumerate([10, 20, 30])
    ]
    result = region.reconcile_rows(report, entries)
    assert result["reconciled"] is True
    assert result["measured_rows"] == 60
    assert result["bbox_scoped_rows"] == 60


def test_reconcile_fails_on_row_mismatch():
    report = scoped_report([10, 20, 30])
    entries = [
        region.task_rows_entry(map_report(rows), task_index=index, name=f"region-{index:03d}")
        for index, rows in enumerate([10, 20, 31])
    ]
    with pytest.raises(region.RegionRehearsalError, match="do not equal plan"):
        region.reconcile_rows(report, entries)


def test_reconcile_fails_when_a_task_is_missing():
    report = scoped_report([10, 20, 30])
    entries = [
        region.task_rows_entry(map_report(rows), task_index=index, name=f"region-{index:03d}")
        for index, rows in enumerate([10, 20])
    ]
    with pytest.raises(region.RegionRehearsalError, match="missing="):
        region.reconcile_rows(report, entries)


def test_reconcile_fails_on_duplicate_task_index():
    report = scoped_report([10, 20])
    entries = [
        region.task_rows_entry(map_report(10), task_index=0, name="region-000"),
        region.task_rows_entry(map_report(20), task_index=0, name="region-000"),
    ]
    with pytest.raises(region.RegionRehearsalError, match="duplicate task_index"):
        region.reconcile_rows(report, entries)


def test_reconcile_fails_on_an_extra_task_not_in_the_plan():
    # An extra task index that the plan never emitted must fail even when the
    # measured rows happen to sum to bbox_scoped_rows, so a stray/misrouted
    # evidence artifact cannot slip a spurious task past reconciliation.
    report = scoped_report([10, 20])
    entries = [
        region.task_rows_entry(map_report(10), task_index=0, name="region-000"),
        region.task_rows_entry(map_report(20), task_index=1, name="region-001"),
        region.task_rows_entry(map_report(0), task_index=2, name="region-002"),
    ]
    with pytest.raises(region.RegionRehearsalError, match=r"extra=\[2\]"):
        region.reconcile_rows(report, entries)


def test_task_rows_rejects_non_integer_measured_rows():
    # A bool is an int subclass; a JSON float would silently coerce sums. Both
    # must be rejected so reconciliation stays exact-integer.
    with pytest.raises(region.RegionRehearsalError, match="non-negative int"):
        region.task_rows_entry(
            {"schema": "overture-address-verified-resume-map-v1",
             "map_fragments": {"input_rows": True}},
            task_index=0,
            name="region-000",
        )
    with pytest.raises(region.RegionRehearsalError, match="non-negative int"):
        region.task_rows_entry(
            {"schema": "overture-address-verified-resume-map-v1",
             "map_fragments": {"input_rows": 10.0}},
            task_index=0,
            name="region-000",
        )


# --- family manifest ------------------------------------------------------


def upload_report(keys_bytes_sha: list[tuple[str, int, str]]):
    return {
        "schema": "overture-verified-shuffle-manifest-v1",
        "artifacts": [
            {"path": f"/tmp/{key.rsplit('/', 1)[-1]}", "key": key, "bytes": nbytes, "sha256": sha}
            for key, nbytes, sha in keys_bytes_sha
        ],
    }


SHA_A = "a" * 64
SHA_B = "b" * 64
KEY_A = "smoke/address-real-shuffle/1-1/family/sha256/" + SHA_A + "/region-000.aidx"
KEY_B = "smoke/address-real-shuffle/1-1/family/sha256/" + SHA_B + "/region-001.aidx"


def test_manifest_built_and_verified_against_listing():
    reports = [upload_report([(KEY_A, 100, SHA_A)]), upload_report([(KEY_B, 200, SHA_B)])]
    listing = {KEY_A: 100, KEY_B: 200}
    manifest = region.build_and_verify_manifest(
        release="2026-06-17.0",
        region_name="us-northeast",
        bbox=[-80.5, 38.0, -66.9, 47.5],
        build_id="c" * 64,
        producer_commit="deadbeef",
        upload_reports=reports,
        listing=listing,
        generated_at=None,
    )
    assert manifest["family"] == "addresses"
    assert manifest["region"]["bbox_scope"] == "row_group_approximate"
    assert manifest["versions"]["format"] == gbm.ADDRESS_FORMAT_VERSION
    assert manifest["versions"]["normalization"] == gbm.ADDRESS_NORMALIZATION_VERSION
    assert manifest["versions"]["tokenizer"] is None
    assert manifest["totals"]["artifacts"] == 2
    # The manifest re-validates deterministically.
    assert gbm.validate_family_manifest(manifest) == manifest


def test_manifest_verify_rejects_size_mismatch():
    reports = [upload_report([(KEY_A, 100, SHA_A)])]
    listing = {KEY_A: 999}
    with pytest.raises(ValueError, match="size mismatch"):
        region.build_and_verify_manifest(
            release="2026-06-17.0",
            region_name="us-northeast",
            bbox=[-80.5, 38.0, -66.9, 47.5],
            build_id="c" * 64,
            producer_commit="deadbeef",
            upload_reports=reports,
            listing=listing,
            generated_at=None,
        )


def test_manifest_verify_rejects_unexpected_object_in_listing():
    reports = [upload_report([(KEY_A, 100, SHA_A)])]
    listing = {KEY_A: 100, KEY_B: 200}
    with pytest.raises(ValueError, match="unexpected objects"):
        region.build_and_verify_manifest(
            release="2026-06-17.0",
            region_name="us-northeast",
            bbox=[-80.5, 38.0, -66.9, 47.5],
            build_id="c" * 64,
            producer_commit="deadbeef",
            upload_reports=reports,
            listing=listing,
            generated_at=None,
        )


def test_manifest_rejects_duplicate_reduce_output_key():
    # Two upload reports naming the same immutable object key must fail rather
    # than double-count one artifact into the family manifest.
    reports = [upload_report([(KEY_A, 100, SHA_A)]), upload_report([(KEY_A, 100, SHA_A)])]
    with pytest.raises(region.RegionRehearsalError, match="duplicate reduce-output key"):
        region.build_and_verify_manifest(
            release="2026-06-17.0",
            region_name="us-northeast",
            bbox=[-80.5, 38.0, -66.9, 47.5],
            build_id="c" * 64,
            producer_commit="deadbeef",
            upload_reports=reports,
            listing={KEY_A: 100},
            generated_at=None,
        )


def test_synth_build_id_is_deterministic_hex():
    first = region.synth_build_id(
        release="2026-06-17.0", region_name="us-northeast", bbox=[-80.5, 38.0, -66.9, 47.5], run_id="42"
    )
    second = region.synth_build_id(
        release="2026-06-17.0", region_name="us-northeast", bbox=[-80.5, 38.0, -66.9, 47.5], run_id="42"
    )
    assert first == second
    assert len(first) == 64 and all(c in "0123456789abcdef" for c in first)


# --- CLI cap-fail exit code ----------------------------------------------


def test_cli_matrix_cap_fail_exits_nonzero(tmp_path):
    report_path = write(tmp_path, "scoped.json", scoped_report([1, 1, 1]))
    code = region.main(
        ["matrix", "--scoped-report", str(report_path), "--region-name", "us-northeast", "--max-tasks", "2"]
    )
    assert code == 2


def test_cli_matrix_and_reconcile_roundtrip(tmp_path):
    report_path = write(tmp_path, "scoped.json", scoped_report([10, 20]))
    matrix_out = tmp_path / "matrix.json"
    summary_out = tmp_path / "summary.json"
    code = region.main(
        [
            "matrix",
            "--scoped-report",
            str(report_path),
            "--region-name",
            "us-northeast",
            "--max-tasks",
            "40",
            "--matrix-out",
            str(matrix_out),
            "--summary-out",
            str(summary_out),
        ]
    )
    assert code == 0
    matrix = json.loads(matrix_out.read_text())
    assert [entry["task_index"] for entry in matrix["include"]] == [0, 1]

    rows_dir = tmp_path / "rows"
    rows_dir.mkdir()
    for index, rows in enumerate([10, 20]):
        region.main(
            [
                "task-rows",
                "--map-report",
                str(write(tmp_path, f"map-{index}.json", map_report(rows))),
                "--task-index",
                str(index),
                "--name",
                f"region-{index:03d}",
                "--out",
                str(rows_dir / f"region-{index:03d}.json"),
            ]
        )
    reconcile_out = tmp_path / "reconcile.json"
    code = region.main(
        [
            "reconcile",
            "--scoped-report",
            str(report_path),
            "--task-rows-dir",
            str(rows_dir),
            "--out",
            str(reconcile_out),
        ]
    )
    assert code == 0
    assert json.loads(reconcile_out.read_text())["measured_rows"] == 30
