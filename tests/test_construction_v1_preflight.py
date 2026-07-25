"""Offline unit proofs for the construction-v1 pre-flight fixes.

No network and no cargo build: these exercise the retry wrapper, the dry-run
validate-only projection paths, the reduce batching arithmetic + capacity
prediction, and the fail-closed remote-marker resume skip.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ROWGROUPS = _load("preflight_rowgroups", "scripts/experiment_hosted_rowgroups.py")
HOSTED = _load("preflight_hosted", "scripts/construction_v1_hosted.py")

ADDRESS_INVENTORY = ROOT / "benchmarks/address-construction-v1-data/inventory/addresses.json"
PLACES_INVENTORY = ROOT / "benchmarks/places-construction-v1-data/inventory/places.json"
CONTROL = _load("preflight_control", "scripts/construction_v1_control.py")
# The evidence spec is pinned in ONE place (construction_v1_control.py); tests
# read it from there so a version bump can never leave a stale literal behind.
PLACES_SPEC = ROOT / CONTROL.FAMILIES["places"]["spec"]


# --------------------------------------------------------------------------- #
# Partition caps: three surfaces that must not drift apart silently
# --------------------------------------------------------------------------- #
PARTITION_CAP_FIELDS = (
    "partition_term_rows",
    "partition_estimated_bytes",
    "partition_distinct_tokens",
)
# The caps the FROZEN places evidence spec v2 declares, and the caps the hosted
# planet build actually runs. They DIFFER, deliberately and with measured
# justification (the 2026-07-22.0 growth test; see the HOSTED_LIMITS comment and
# docs/plans/2026-07-24-construction-v1-follow-ups.md). Pinned here so neither
# side can move without this test naming the other one: the spec caps used to be
# read by no code at all, which is how the build came to exceed them unnoticed.
SPEC_V2_PARTITION_CAPS = {
    "partition_term_rows": 1_000_000,
    "partition_estimated_bytes": 268_435_456,
    "partition_distinct_tokens": 250_000,
}
HOSTED_PARTITION_CAPS = {
    "partition_term_rows": 2_000_000,
    "partition_estimated_bytes": 512 * 1024**2,
    "partition_distinct_tokens": 400_000,
}


def test_evidence_spec_partition_caps_are_read_by_the_rehearsal():
    # The spec's three partition hard caps were dead declarations: the rehearsal
    # restated the same numbers as literals, so the spec text was unchecked and
    # the rehearsal was free to drift off the spec it produces evidence under.
    # It now READS them, and the values still match what v2 froze.
    pytest.importorskip("pyarrow")
    rehearse = _load("preflight_rehearse", "scripts/rehearse_places_construction_v1.py")
    assert rehearse.EVIDENCE_SPEC == PLACES_SPEC
    assert rehearse.spec_partition_caps() == SPEC_V2_PARTITION_CAPS
    declared = json.loads(PLACES_SPEC.read_text())["acceptance_gates"]["map_reduce"]
    assert declared["partition_term_rows_hard_cap"] == 1_000_000
    assert declared["partition_estimated_uncompressed_bytes_hard_cap"] == 268_435_456
    assert declared["partition_distinct_tokens_hard_cap"] == 250_000


def test_places_limits_defaults_are_the_hosted_production_caps():
    # The dataclass defaults were left behind when the hosted caps were raised, so
    # every caller that is not the hosted CLI planned at caps the planet build no
    # longer uses. Defaults and hosted limits are now one value per cap.
    limits = HOSTED.PLACES.Limits()
    for field in PARTITION_CAP_FIELDS:
        assert getattr(limits, field) == HOSTED_PARTITION_CAPS[field] == \
            HOSTED.HOSTED_LIMITS["places"][field], field


def test_hosted_partition_caps_exceed_the_frozen_spec_caps_by_declaration():
    # This is the deliberate divergence, asserted rather than assumed: raising the
    # rehearsal to these values would break spec v2's "relaxation_policy: none"
    # and its adaptive-subdivision coverage gate, so closing the gap needs a
    # places evidence spec v3, not an edit.
    for field in PARTITION_CAP_FIELDS:
        assert HOSTED_PARTITION_CAPS[field] > SPEC_V2_PARTITION_CAPS[field], field


# --------------------------------------------------------------------------- #
# P1-3: bounded retry wrapper
# --------------------------------------------------------------------------- #
class _FakeResponse:
    def __init__(self, body: bytes, headers: dict[str, str]):
        self._body = body
        self.headers = _FakeHeaders(headers)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


class _FakeHeaders:
    def __init__(self, headers: dict[str, str]):
        self._headers = headers

    def items(self):
        return self._headers.items()


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://example/x", code, "boom", {}, io.BytesIO(b""))


def test_retry_wrapper_retries_5xx_then_succeeds():
    calls = {"n": 0}
    slept: list[float] = []

    def opener(request, timeout):
        calls["n"] += 1
        if calls["n"] < 3:
            raise _http_error(503)
        return _FakeResponse(b"ok", {"ETag": '"abc"', "Content-Length": "2"})

    body, headers = ROWGROUPS.urlopen_with_retry(
        object(), timeout=1, sleep=slept.append, opener=opener
    )
    assert body == b"ok"
    assert headers["ETag"] == '"abc"'
    assert calls["n"] == 3
    # Exponential backoff: 1s then 2s before the third (successful) attempt.
    assert slept == [1.0, 2.0]


def test_retry_wrapper_does_not_retry_4xx():
    def opener(request, timeout):
        raise _http_error(404)

    with pytest.raises(urllib.error.HTTPError):
        ROWGROUPS.urlopen_with_retry(object(), timeout=1, sleep=lambda _s: None, opener=opener)


def test_retry_wrapper_fails_closed_after_exhausting_attempts():
    def opener(request, timeout):
        raise urllib.error.URLError("down")

    with pytest.raises(RuntimeError) as excinfo:
        ROWGROUPS.urlopen_with_retry(
            object(), timeout=1, attempts=4, sleep=lambda _s: None, opener=opener
        )
    assert "after 4 attempts" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# P1-1: validate-only projection paths (no S3)
# --------------------------------------------------------------------------- #
def test_address_validate_only_reads_no_s3():
    import argparse

    args = argparse.Namespace(
        release="2026-06-17.0", family="addresses", inventory_report=ADDRESS_INVENTORY,
        task_index=0, max_rows=4_000_000, max_groups=72,
        target_rowgroup_uncompressed_bytes=400_000_000, json_out=None,
    )
    result = ROWGROUPS.validate_only(args)
    assert result["s3_accessed"] is False
    assert result["planned_rows"] > 0
    assert result["would_read_ranges"]
    assert all(r["uri"].startswith("s3://") for r in result["would_read_ranges"])


def test_places_validate_only_reads_no_s3():
    import argparse

    places = _load("preflight_places_projector", "scripts/project_places_construction_v1.py")
    args = argparse.Namespace(
        inventory=PLACES_INVENTORY, evidence_spec=PLACES_SPEC, task_index=0,
        max_rows=4_000_000, max_groups=72,
        max_selected_compressed_bytes=536_870_912,
        max_selected_uncompressed_bytes=1_000_000_000, report=None,
    )
    result = places.validate_only(args)
    assert result["s3_accessed"] is False
    assert result["expected_input_records"] > 0
    assert result["would_read_ranges"]


# --------------------------------------------------------------------------- #
# source-limits: correct per-object transform bound from the projection report
# --------------------------------------------------------------------------- #
def test_source_limits_addresses_are_per_object_upper_bounds(tmp_path):
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"sources": [
        {"source_object_index": 0, "parquet_rows": 4717270, "parquet_row_groups": 256},
        {"source_object_index": 2, "parquet_rows": 100, "parquet_row_groups": 5},
    ]}))
    out = tmp_path / "limits.json"
    assert HOSTED.main(["source-limits", "--report", str(report),
                        "--family", "addresses", "--output", str(out)]) == 0
    objects = json.loads(out.read_text())["objects"]
    # object_index 0 and 2 present; index 1 gets a positive placeholder.
    assert objects == [
        {"records": 4717270, "row_groups": 256},
        {"records": 1, "row_groups": 1},
        {"records": 100, "row_groups": 5},
    ]
    # No object bound is zero (the transform bails on a zero bound).
    assert all(o["records"] > 0 and o["row_groups"] > 0 for o in objects)


def test_source_limits_places_map_selected_objects_by_index(tmp_path):
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"identity": {
        "ranges": [{"object_index": 0}, {"object_index": 0}],
        "objects": [{"records": 988713, "row_group_count": 54}],
    }}))
    out = tmp_path / "limits.json"
    assert HOSTED.main(["source-limits", "--report", str(report),
                        "--family", "places", "--output", str(out)]) == 0
    objects = json.loads(out.read_text())["objects"]
    assert objects == [{"records": 988713, "row_groups": 54}]


# --------------------------------------------------------------------------- #
# P0-3: reduce batching arithmetic + capacity prediction
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "partitions,job_cap,expected_jobs",
    [
        (1, 128, 1),
        (128, 128, 128),
        (129, 128, 65),   # batch_size 2 -> ceil(129/2)=65 jobs, all <= cap
        (474, 128, 119),  # planet addresses: batch_size 4 -> 119 jobs <= 256/128
        (2421, 128, 128),  # planet places: batch_size 19 -> 128 jobs
        (300, 256, 150),  # batch_size 2 -> 150 jobs
    ],
)
def test_reduce_batches_never_exceed_the_job_cap(partitions, job_cap, expected_jobs):
    batch_size, batches = HOSTED._reduce_batches(partitions, job_cap=job_cap)
    assert len(batches) == expected_jobs
    assert len(batches) <= job_cap
    # Contiguous, complete, non-overlapping cover of every partition exactly once.
    covered = []
    for batch in batches:
        covered.extend(range(batch["partition_start"], batch["partition_start"] + batch["partition_count"]))
    assert covered == list(range(partitions))
    assert all(batch["partition_count"] <= batch_size for batch in batches)


def _contract(tmp_path: Path, max_reducers: int = 128) -> Path:
    request = tmp_path / "request.json"
    request.write_text(json.dumps({
        "schema": "overture-construction-v1-request-v1", "release": "2026-06-17.0",
        "families": {"addresses": {}, "places": {}},
        "versions": {"duckdb": "1.5.1", "pyarrow": "25.0.0", "numpy": "2.3.5",
                     "python": "3.12.12", "rustc": "test"},
        "caps": {"max_reducers_per_family": max_reducers,
                 "max_remote_operations": 100000, "max_remote_write_bytes": 1_000_000_000_000},
        "namespaces": {"immutable_root": "construction-v1/deadbeef",
                       "slice": "construction-v1/deadbeef/slice/slice-x/",
                       "markers": "construction-v1/deadbeef/markers/"},
    }) + "\n")
    contract = tmp_path / "contract.json"
    runtime = tmp_path / "runtime.json"
    assert HOSTED.main(["derive-contract", "--request", str(request),
                        "--output", str(contract), "--runtime", str(runtime),
                        "--allow-unpinned-duckdb"]) == 0
    return contract


def _ledger(tmp_path: Path, cap: int, prior: int, spent: int) -> Path:
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({
        "schema": "construction-v1-run-ledger-v1", "max_total_runner_minutes": cap,
        "prior_runner_minutes": prior,
        "phases": [{"phase": "map", "runner_minutes": spent}],
    }))
    return ledger


def test_predict_reduce_addresses_needs_batching_but_fits_budget(tmp_path, capsys):
    contract = _contract(tmp_path)
    ledger = _ledger(tmp_path, cap=40_000, prior=0, spent=3000)
    assert HOSTED.main([
        "predict-reduce", "--contract", str(contract), "--family", "addresses",
        "--inventory", str(ADDRESS_INVENTORY), "--ledger", str(ledger),
        "--tail-minutes", "210",
    ]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["predicted_partitions"] >= 474  # exceeds the 256 matrix cap raw
    assert out["reduce_job_count"] <= 128       # but batches under the cap
    assert out["within_cap"] is True
    # The projection reports the measured per-partition timing it used and each
    # batch job fits the reduce job timeout.
    timing = out["timing_assumption"]
    assert timing["measured_reduce_minutes_per_partition"] > 0
    assert out["reduce_batch_size"] <= timing["timeout_max_batch"]
    assert timing["per_job_minutes"] <= timing["job_timeout_minutes"]


def test_predict_reduce_addresses_floors_on_the_per_country_bisection(tmp_path, capsys):
    # The addresses branch divided records by a row cap with NO structural floor --
    # the same defect PR #155 fixed on the places branch. The address planner
    # bisects each country independently, so every country contributes at least one
    # partition and an over-cap country's leaf count is a power of two. On the
    # planet inventory the row-derived figure is 474 and the real shape is 725, so
    # the gate was ~1.5x optimistic (not 14x: the address floor is 34 countries,
    # far below the row-derived figure, unlike Places' 16,633 cells).
    contract = _contract(tmp_path)
    assert HOSTED.main([
        "predict-reduce", "--contract", str(contract), "--family", "addresses",
        "--inventory", str(ADDRESS_INVENTORY),
    ]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    inventory = json.loads(ADDRESS_INVENTORY.read_text())
    row_cap = HOSTED.HOSTED_LIMITS["addresses"]["max_pack_rows"]
    by_rows = -(-HOSTED._inventory_total_records(inventory) // row_cap)
    floor, _basis = HOSTED._address_structural_partitions(inventory, row_cap)
    assert by_rows == 474 and floor == 725
    assert out["predicted_partitions"] == floor > by_rows
    assert "per-country hash bisection" in out["prediction_basis"]


def test_address_structural_floor_counts_every_country_and_rounds_to_powers_of_two():
    # Both effects the row division misses, isolated. Two countries of one row each
    # are two partitions, not one; a country at 3x the cap needs four leaves,
    # not three, because the split halves.
    floor, basis = HOSTED._address_structural_partitions(
        {"exact_country_rows": {"AA": 1, "BB": 1}, "totals": {"records": 2}}, 1_000_000
    )
    assert floor == 2 and "2 inventory countries" in basis
    floor, _basis = HOSTED._address_structural_partitions(
        {"exact_country_rows": {"AA": 3_000_000}, "totals": {"records": 3_000_000}},
        1_000_000,
    )
    assert floor == 4


def test_address_structural_floor_fails_closed_without_per_country_rows():
    # Falling back to the row-derived figure would silently restore the optimism.
    with pytest.raises(SystemExit, match="exact_country_rows"):
        HOSTED._address_structural_partitions({"totals": {"records": 10}}, 1_000_000)


def test_predict_reduce_fails_closed_when_minutes_exceed_cap(tmp_path):
    contract = _contract(tmp_path)
    # A tiny cap the batched reduce projection cannot fit.
    ledger = _ledger(tmp_path, cap=1000, prior=0, spent=900)
    with pytest.raises(SystemExit) as excinfo:
        HOSTED.main([
            "predict-reduce", "--contract", str(contract), "--family", "places",
            "--inventory", str(PLACES_INVENTORY), "--ledger", str(ledger),
            "--tail-minutes", "210",
        ])
    assert "exceed cap" in str(excinfo.value)


def test_reduce_batching_fails_closed_when_a_batch_cannot_fit_the_job_timeout():
    # If the measured per-partition reduce time is so large that the batch a legal
    # matrix requires cannot serially fit one job's timeout, batching fails closed
    # rather than dispatch a job that would time out.
    with pytest.raises(SystemExit) as excinfo:
        HOSTED._reduce_batches(100_000, job_cap=128, timeout_max_batch=10)
    assert "failing closed" in str(excinfo.value)


def test_reduce_batching_reports_measured_timing_that_fits_the_timeout():
    # Planet Places (2421 predicted partitions) at the measured 1.0 min/partition
    # batches to 19 partitions/job -> ~19 min << the 330-min job timeout.
    batch_size, batches = HOSTED._reduce_batches(2421, job_cap=128, timeout_max_batch=165)
    assert batch_size == 19 and len(batches) == 128


# --------------------------------------------------------------------------- #
# Places reduce jobs own a SHUFFLE-BUCKET RANGE
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bits,job_cap", [(8, 256), (8, 128), (8, 100), (4, 16), (4, 3)])
def test_reduce_bucket_ranges_cover_the_bucket_space_exactly_once(bits, job_cap):
    # Ownership is exact only if the ranges are a TOTAL, DISJOINT cover of the
    # bucket space -- a gap silently drops every cell that hashed into it.
    buckets = 1 << bits
    counts = [(index % 3) for index in range(buckets)]
    stride, ranges = HOSTED._reduce_bucket_ranges(counts, job_cap=job_cap)
    assert len(ranges) <= job_cap
    covered = []
    for item in ranges:
        assert item["bucket_start"] <= item["bucket_end"]
        covered.extend(range(item["bucket_start"], item["bucket_end"] + 1))
    assert covered == list(range(buckets))
    assert stride >= 1
    # Partition offsets are contiguous, which is what lets a bucket range double
    # as the partition range the plan and the workflow already speak in.
    offset = 0
    for item in ranges:
        assert item["partition_start"] == offset
        assert item["partition_count"] == sum(
            counts[item["bucket_start"] : item["bucket_end"] + 1]
        )
        offset += item["partition_count"]
    assert offset == sum(counts)


def test_reduce_bucket_ranges_fail_closed_when_one_bucket_cannot_fit_a_job():
    # A bucket is INDIVISIBLE -- a cell never splits across buckets -- so there is
    # no smaller stride to fall back on. Failing closed here is the honest answer;
    # silently dispatching a job that times out is not.
    counts = [400] + [0] * 255
    with pytest.raises(SystemExit) as excinfo:
        HOSTED._reduce_bucket_ranges(counts, job_cap=256, timeout_max_batch=165)
    assert "indivisible" in str(excinfo.value)


def test_places_reduce_execution_is_bucket_ranges_over_the_plan():
    bits = 4
    partitions = [
        {"partition_cell": cell, "shuffle_bucket": HOSTED.PLACES.shuffle_bucket(
            HOSTED.PLACES.cell_partition_key(cell), bits)}
        for cell in ("0000", "0001", "0002", "00ff", "b2e3", "5e5e", "ffff")
    ]
    partitions.sort(key=lambda item: (item["shuffle_bucket"], item["partition_cell"]))
    batch_size, dispatched, details = HOSTED._places_reduce_execution(
        partitions, bits=bits, job_cap=16, timeout_max_batch=165
    )
    assert details["bucket_count"] == 16 and details["bucket_stride"] == 1
    # Only populated ranges are dispatched -- an empty range would read nothing --
    # but every partition is still owned by exactly one of them.
    assert sum(item["partition_count"] for item in dispatched) == len(partitions)
    assert [item["batch_index"] for item in dispatched] == list(range(len(dispatched)))
    assert batch_size == max(item["partition_count"] for item in dispatched)
    assert details["populated_bucket_ranges"] == len(dispatched)


def test_places_reduce_execution_rejects_map_buckets_no_range_covers():
    # Empty ranges are dropped from the matrix, so a bucket holding map fragments
    # but no plan partition would never be dispatched and neither in-job guard
    # would run. Catch it here, where the markers are still in hand.
    bits = 4
    partitions = [
        {"partition_cell": "0000", "shuffle_bucket": HOSTED.PLACES.shuffle_bucket(
            HOSTED.PLACES.cell_partition_key("0000"), bits)}
    ]
    covered = partitions[0]["shuffle_bucket"]
    orphan = (covered + 1) % 16
    with pytest.raises(SystemExit) as excinfo:
        HOSTED._places_reduce_execution(
            partitions, bits=bits, job_cap=16, timeout_max_batch=None,
            fragment_buckets={covered, orphan},
        )
    assert "no dispatched" in str(excinfo.value)
    # The same call with only covered buckets is fine.
    HOSTED._places_reduce_execution(
        partitions, bits=bits, job_cap=16, timeout_max_batch=None,
        fragment_buckets={covered},
    )


def test_places_reduce_execution_rejects_a_plan_not_ordered_by_bucket():
    bits = 8
    ordered = sorted(
        ("0000", "b2e3", "5e5e", "ffff"),
        key=lambda cell: HOSTED.PLACES.shuffle_bucket(
            HOSTED.PLACES.cell_partition_key(cell), bits
        ),
    )
    partitions = [{"partition_cell": cell} for cell in reversed(ordered)]
    with pytest.raises(SystemExit) as excinfo:
        HOSTED._places_reduce_execution(
            partitions, bits=bits, job_cap=256, timeout_max_batch=None
        )
    assert "ordered by shuffle bucket" in str(excinfo.value)


def test_predict_reduce_places_plans_bucket_ranges(tmp_path, capsys):
    contract = _contract(tmp_path)
    assert HOSTED.main([
        "predict-reduce", "--contract", str(contract), "--family", "places",
        "--inventory", str(PLACES_INVENTORY),
    ]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["reduce_ownership"] == "shuffle-bucket-range"
    assert out["bucket_count"] == 1 << HOSTED.PLACES.SHUFFLE_BUCKET_BITS
    # The committed partition-plan floor (PR #155) still supplies the partition
    # count; only the way jobs are cut changed.
    assert "committed plan" in out["prediction_basis"]
    assert out["reduce_job_count"] == out["populated_bucket_ranges"]
    assert out["reduce_job_count"] <= out["reduce_job_cap"]
    assert out["reduce_batch_size"] <= out["timing_assumption"]["timeout_max_batch"]


# --------------------------------------------------------------------------- #
# P1-4: fail-closed remote-marker resume skip
# --------------------------------------------------------------------------- #
def _admit(tmp_path: Path, store: Path, remote: Path | None, contract: Path):
    out = tmp_path / "admit.json"
    argv = ["admit-task", "--store-root", str(store), "--family", "addresses",
            "--phase", "map", "--task-id", "addresses-map-000", "--output", str(out),
            "--contract", str(contract)]
    if remote is not None:
        argv += ["--remote-root", str(remote)]
    assert HOSTED.main(argv) == 0
    return json.loads(out.read_text())


def test_admit_task_skips_when_remote_marker_present(tmp_path):
    contract = _contract(tmp_path)
    store = tmp_path / "store"
    remote = tmp_path / "remote"
    # No local store, no remote marker yet -> not completed (task must run).
    assert _admit(tmp_path, store, remote, contract)["completed"] is False
    # Publish the durable create-only marker; a fresh resume dispatch now skips.
    marker_key = HOSTED.ADDRESS.marker_key("addresses-map-000")
    marker_path = remote / marker_key
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(json.dumps({"completed": True}))
    result = _admit(tmp_path, store, remote, contract)
    assert result["completed"] is True
    assert result["remote_completed"] is True
    assert result["local_completed"] is False


def test_admit_task_absent_remote_marker_is_not_completed(tmp_path):
    contract = _contract(tmp_path)
    store = tmp_path / "store"
    remote = tmp_path / "remote"
    remote.mkdir()
    # Definitive absence -> not completed (safe to re-run under create-only).
    assert _admit(tmp_path, store, remote, contract)["completed"] is False


def test_admit_task_remote_head_transport_error_aborts(tmp_path, monkeypatch):
    contract = _contract(tmp_path)
    store = tmp_path / "store"
    remote = tmp_path / "remote"
    remote.mkdir()

    def boom(self, key):
        raise OSError("remote HEAD transport failure")

    monkeypatch.setattr(HOSTED.REMOTE.FilesystemRemote, "head", boom)
    # Fail-closed direction: a transport error ABORTS rather than re-running.
    with pytest.raises(SystemExit) as excinfo:
        _admit(tmp_path, store, remote, contract)
    assert "aborting rather than re-running" in str(excinfo.value)
