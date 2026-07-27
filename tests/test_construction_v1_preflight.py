"""Offline unit proofs for the construction-v1 pre-flight fixes.

No network and no cargo build: these exercise the retry wrapper, the dry-run
validate-only projection paths, the reduce batching arithmetic + capacity
prediction, and the fail-closed remote-marker resume skip.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import inspect
import io
import json
import re
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
    # NOT divergent: the Places serving encoder's MAX_INDEX_ENTRIES is 250,000 and a
    # routed artifact's index-entry count is exactly its partition's distinct-token
    # count, so this cap is bounded by the encoder and happens to land on the same
    # value the frozen spec declares. See
    # tests/test_construction_v1_publication_budget.py, which pins it to the Rust
    # constant, and the HOSTED_LIMITS comment for why 400,000 was wrong.
    "partition_distinct_tokens": 250_000,
}
# The two caps that genuinely exceed the frozen spec's hard caps.
SPEC_DIVERGENT_CAP_FIELDS = ("partition_term_rows", "partition_estimated_bytes")


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


@pytest.mark.parametrize("value", [None, True, 0, -1, "1000000", 1.5])
def test_spec_partition_caps_fail_closed_on_an_unusable_declaration(tmp_path, value):
    # A spec missing a cap, or declaring `true` for one (which is an `int` in
    # Python and would silently become a cap of 1), must not produce a cap.
    pytest.importorskip("pyarrow")
    rehearse = _load("preflight_rehearse", "scripts/rehearse_places_construction_v1.py")
    spec = json.loads(PLACES_SPEC.read_text())
    gates = spec["acceptance_gates"]["map_reduce"]
    if value is None:
        del gates["partition_term_rows_hard_cap"]
    else:
        gates["partition_term_rows_hard_cap"] = value
    tampered = tmp_path / "spec.json"
    tampered.write_text(json.dumps(spec))
    with pytest.raises(ValueError, match="partition_term_rows_hard_cap"):
        rehearse.spec_partition_caps(tampered)


def test_places_limits_defaults_are_the_hosted_production_caps():
    # The dataclass defaults were left behind when the hosted caps were raised, so
    # every caller that is not the hosted CLI planned at caps the planet build no
    # longer uses. They are still separate literals in separate files -- this test
    # is what keeps them equal.
    limits = HOSTED.PLACES.Limits()
    for field in PARTITION_CAP_FIELDS:
        assert getattr(limits, field) == HOSTED_PARTITION_CAPS[field] == \
            HOSTED.HOSTED_LIMITS["places"][field], field


def test_hosted_partition_caps_exceed_the_frozen_spec_caps_by_declaration():
    # This is the deliberate divergence, asserted rather than assumed: raising the
    # rehearsal to these values would break spec v2's "relaxation_policy: none"
    # and its adaptive-subdivision coverage gate, so closing the gap needs a
    # places evidence spec v3, not an edit.
    for field in SPEC_DIVERGENT_CAP_FIELDS:
        assert HOSTED_PARTITION_CAPS[field] > SPEC_V2_PARTITION_CAPS[field], field
    # And the third cap is NOT part of that divergence any more. It was raised to
    # 400,000 with the other two, which put it over the serving encoder's hard
    # MAX_INDEX_ENTRIES; it is back at the encoder's value, which is also the value
    # the spec declares. Pinned as an equality so a future "raise the divergent
    # caps" edit cannot quietly take this one with it.
    assert (
        HOSTED_PARTITION_CAPS["partition_distinct_tokens"]
        == SPEC_V2_PARTITION_CAPS["partition_distinct_tokens"]
        == HOSTED.PLACES.SERVING_MAX_INDEX_ENTRIES
    )
    assert set(PARTITION_CAP_FIELDS) - set(SPEC_DIVERGENT_CAP_FIELDS) == {
        "partition_distinct_tokens"
    }


# --------------------------------------------------------------------------- #
# Head-merge caps: the same three surfaces, one phase later
# --------------------------------------------------------------------------- #
# The frozen spec's head declarations, and the values the hosted build runs. The spec
# declares ONE candidate-row cap and the code has two enforcement sites, so the rehearsal
# applies the spec's value to both.
SPEC_V2_HEAD_CAPS = {
    "max_head_candidate_rows": 5_000_000,
    "max_task_head_candidate_rows": 5_000_000,
    "head_merge_fan_in": 16,
}
HOSTED_HEAD_CAPS = {
    # Raised off MEASURED Europe volume: 26,168,687 candidate rows for 43.9% of the
    # planet, i.e. 5.2x the spec's 5,000,000, so that value does not admit a planet head.
    "max_head_candidate_rows": 200_000_000,
    "max_task_head_candidate_rows": 6_000_000,
    # Diverges DOWNWARD: a fan-in at or above the map task count gives the tree one
    # stage, which is the merge it was introduced to replace.
    "head_merge_fan_in": 8,
    "head_shard_copy_batch": 256,
}
# Europe, 43.9% of the planet, 36 map tasks at production per-task granularity. These are
# MEASURED and they are what the caps are sized against; the Monaco-linear 134.3M figure
# they refute is recorded in the docs as refuted.
EUROPE_CANDIDATE_ROWS = 26_168_687
EUROPE_CANDIDATE_ROWS_PER_PLACE = 0.8045
PLANET_CANDIDATE_ROWS_FLOOR = 59_700_000
PLANET_CANDIDATE_ROWS_UPPER = 120_000_000


def test_evidence_spec_head_caps_are_read_by_the_rehearsal():
    # `maximum_head_candidate_rows` and `maximum_merge_fan_in` were satisfied by
    # COINCIDENCE, not by reading: the rehearsal inherited the 5,000,000 dataclass
    # default and the tree merge was called with `max_fan_in_tasks`, which the
    # rehearsal happened to set to 16. The second coincidence is gone now that the
    # hosted build runs a fan-in of 8, so the rehearsal reads the spec.
    pytest.importorskip("pyarrow")
    rehearse = _load("preflight_rehearse", "scripts/rehearse_places_construction_v1.py")
    assert rehearse.spec_head_caps() == SPEC_V2_HEAD_CAPS
    declared = json.loads(PLACES_SPEC.read_text())["acceptance_gates"]["head"]
    assert declared["maximum_head_candidate_rows"] == 5_000_000
    assert declared["maximum_merge_fan_in"] == 16


@pytest.mark.parametrize("value", [None, True, 0, -1, "5000000", 1.5])
def test_spec_head_caps_fail_closed_on_an_unusable_declaration(tmp_path, value):
    pytest.importorskip("pyarrow")
    rehearse = _load("preflight_rehearse", "scripts/rehearse_places_construction_v1.py")
    spec = json.loads(PLACES_SPEC.read_text())
    gates = spec["acceptance_gates"]["head"]
    if value is None:
        del gates["maximum_merge_fan_in"]
    else:
        gates["maximum_merge_fan_in"] = value
    tampered = tmp_path / "spec.json"
    tampered.write_text(json.dumps(spec))
    with pytest.raises(ValueError, match="maximum_merge_fan_in"):
        rehearse.spec_head_caps(tampered)


def test_places_head_limits_defaults_are_the_hosted_production_caps():
    # Same reason as the partition caps: these are separate literals in separate
    # files, and every caller that is not the hosted CLI plans at the dataclass
    # default. This test is what keeps the two equal.
    limits = HOSTED.PLACES.Limits()
    for field in HOSTED_HEAD_CAPS:
        assert getattr(limits, field) == HOSTED_HEAD_CAPS[field] == \
            HOSTED.HOSTED_LIMITS["places"][field], field
    # And the contract really does carry every one of them through to a phase, which is
    # the only path a hosted run's limits travel.
    contract = {"limits": {"places": dict(HOSTED.HOSTED_LIMITS["places"])}}
    resolved = HOSTED._limits_for(contract, "places")
    for field, expected in HOSTED_HEAD_CAPS.items():
        assert getattr(resolved, field) == expected, field


def test_hosted_head_caps_diverge_from_the_frozen_spec_by_declaration():
    # The fan-in diverges DOWNWARD -- smaller than the spec's maximum, because a
    # fan-in at or above the map task count gives the tree a single stage.
    assert (
        HOSTED_HEAD_CAPS["head_merge_fan_in"] < SPEC_V2_HEAD_CAPS["head_merge_fan_in"]
    )
    # Both candidate-row caps diverge UPWARD, and the global one has to: the spec's
    # 5,000,000 is 5.2x BELOW the Europe candidate set alone, so it does not admit a
    # planet head phase at all -- main would have refused that run at admission.
    for field in ("max_head_candidate_rows", "max_task_head_candidate_rows"):
        assert HOSTED_HEAD_CAPS[field] > SPEC_V2_HEAD_CAPS[field], field
    assert EUROPE_CANDIDATE_ROWS > 5 * SPEC_V2_HEAD_CAPS["max_head_candidate_rows"]
    # The global cap admits the measured planet floor and the CJK-inclusive upper end.
    assert HOSTED_HEAD_CAPS["max_head_candidate_rows"] > PLANET_CANDIDATE_ROWS_UPPER
    assert HOSTED_HEAD_CAPS["max_head_candidate_rows"] > PLANET_CANDIDATE_ROWS_FLOOR
    # And it stays a GUARD: within 2x of the upper end, not an arbitrary number.
    assert HOSTED_HEAD_CAPS["max_head_candidate_rows"] < 2 * PLANET_CANDIDATE_ROWS_UPPER
    # The floor is Europe's MEASURED rate over the planet place count, not a projection
    # from a 38k slice -- recomputed here so the two cannot drift apart.
    assert round(74_223_561 * EUROPE_CANDIDATE_ROWS_PER_PLACE) == pytest.approx(
        PLANET_CANDIDATE_ROWS_FLOOR, rel=0.01
    )
    # The per-task cap must stay far below the global one, or it is a restatement of it
    # rather than a bound on a single pathological task.
    assert HOSTED_HEAD_CAPS["max_head_candidate_rows"] >= 30 * HOSTED_HEAD_CAPS[
        "max_task_head_candidate_rows"
    ]
    # ... and it must still admit the worst planet task: ~671k mean, ~1.34M for a CJK
    # task at 2x term fan-out, ~2.7M pessimistic.
    assert HOSTED_HEAD_CAPS["max_task_head_candidate_rows"] > 2 * 1_340_000
    with pytest.raises(ValueError, match="fan-in must be at least 2"):
        dataclasses.replace(HOSTED.PLACES.Limits(), head_merge_fan_in=1).validate()
    with pytest.raises(ValueError, match="below the per-task cap"):
        dataclasses.replace(
            HOSTED.PLACES.Limits(), max_head_candidate_rows=1_000
        ).validate()


def test_every_production_duckdb_connection_caps_its_temp_directory():
    """`temp_directory` without `max_temp_directory_size` is an uncapped spill.

    DuckDB's default for `max_temp_directory_size` is **90% of available disk**, so a
    spilling query is licensed to fill the runner to within 10% -- outside every cap
    this pipeline declares, and inside the same workspace the stage's inputs and outputs
    live in. Measured on a partial planet head probe: 3.5 GB across 10
    `duckdb_temp_storage_*.tmp` files at 26% completion, a term no projection carried.

    Asserted as a PAIRING over both family modules, by source, because this is the
    per-family-parity defect class: a fix landing on places while addresses silently
    keeps the hole is the failure this test exists to prevent. Every `SET
    temp_directory` must be accompanied by a `SET max_temp_directory_size`, and the size
    must come from the shared derivation rather than a fresh literal.
    """
    for relative in (
        "scripts/places_construction_v1.py",
        "scripts/address_construction_v1.py",
    ):
        source = (ROOT / relative).read_text()
        # Only lines that EXECUTE the setting, so prose about it does not count. The
        # first version of this test counted substrings and was fooled by its own
        # explanatory comment.
        statements = [
            line
            for line in source.splitlines()
            if "SET temp_directory" in line or "SET max_temp_directory_size" in line
            if "connection.execute" in line or line.strip().startswith('f"SET ')
        ]
        directories = sum("SET temp_directory" in line for line in statements)
        caps = sum("SET max_temp_directory_size" in line for line in statements)
        assert directories > 0, relative
        assert caps == directories, (
            f"{relative}: {directories} `SET temp_directory` against {caps} "
            "`SET max_temp_directory_size` -- every spill directory needs a cap"
        )
        # Derived, not a literal: the only legal size is the shared helper's.
        assert "duckdb_temp_limit(limits.max_scratch_bytes)" in source, relative

    # And the derivation itself: a declared share of the stage's disk budget.
    address = _load("preflight_address", "scripts/address_construction_v1.py")
    cap = HOSTED.HOSTED_LIMITS["places"]["max_scratch_bytes"]
    assert address.duckdb_temp_limit(cap) == f"{cap // address.DUCKDB_TEMP_SHARE}B"
    assert address.duckdb_temp_limit(cap, share=2) == f"{cap // 2}B"
    # Spill is ONE term inside the scratch budget, so it must be a fraction of it.
    assert address.DUCKDB_TEMP_SHARE >= 2
    with pytest.raises(ValueError):
        address.duckdb_temp_limit(0)
    with pytest.raises(ValueError):
        address.duckdb_temp_limit(cap, share=1)

    # The global Places head is the measured exception: run 30226086949 exhausted
    # the generic quarter-share cap while the independent whole-stage 17 GiB guard
    # remained healthy.
    places = (ROOT / "scripts/places_construction_v1.py").read_text()
    assert "duckdb_temp_limit(limits.max_scratch_bytes, share=2)" in places


def test_stage_watchdog_poll_is_bounded_by_its_own_sweep_cost():
    """The watchdog must not spend unbounded time measuring the thing it guards.

    `disk_bytes` is an rglob + stat over the whole tree, so its cost scales with the
    file count: 1.51 s per sweep over a 4,096-partition head workspace holding ~1.7M
    files, against a 10 ms intended interval. That burns hours of stat() AND makes the
    guard blind -- a subprocess can start and exit inside one sweep. The poll now waits
    at least as long as the worst sweep took, capping the duty cycle at ~50%.
    """
    address = _load("preflight_address", "scripts/address_construction_v1.py")
    source = inspect.getsource(address.StageWatchdog._run)
    assert "self.stop.wait(" in source
    # The interval is derived from the observed sweep cost, not a constant.
    assert "peak_sweep_seconds" in source, source
    assert "self.stop.wait(0.01)" not in source, source
    # And the achieved resolution is REPORTED rather than assumed, so a guard that
    # observed almost nothing is visible in the evidence instead of reading as coverage.
    reported = set(
        re.findall(r'"(\w+)":', inspect.getsource(address.StageWatchdog.evidence))
    )
    assert {"observations", "peak_sweep_seconds"} <= reported, reported

    # `run_bounded` has the SAME loop and it is the one that runs 4,096 times in the
    # Places head phase -- once per encoder subprocess -- so it gets the same treatment.
    # A fixed 5 ms sleep against a 1.51 s sweep is a 100% duty cycle on stat() AND a
    # 1.5 s blind window per check.
    bounded = inspect.getsource(address.run_bounded)
    assert "time.sleep(0.005)" not in bounded, bounded
    assert "peak_sweep" in bounded, bounded
    assert '"peak_sweep_seconds": peak_sweep' in bounded, bounded


def test_scratch_cap_is_below_the_job_free_disk_floor_for_both_families():
    """A scratch cap ABOVE the disk the job guarantees cannot fire before ENOSPC.

    24 GiB (25,769,803,776 B) sat 170 MB above the 25,600,000,000-byte floor every
    non-reduce job asserts, so every scratch guard built on it -- the head phase's and
    address reduce's alike -- was unreachable: the filesystem filled first and the
    failure surfaced as ENOSPC or as `run_bounded`'s bare "child scratch exceeded its
    hard cap" from whichever subprocess was running, naming no phase and no knob.

    Pinned against the floor PARSED OUT OF THE WORKFLOW, not a restated literal, so
    raising one without the other breaks this test.
    """
    workflow = (ROOT / ".github/workflows/construction-v1.yml").read_text()
    floors = sorted(
        {
            int(value) * 1024
            for value in re.findall(
                r"df -Pk / \| awk 'NR==2 \{print \$4\}'\)\" -ge (\d+)", workflow
            )
        }
    )
    assert floors, "no free-disk floor found in construction-v1.yml"
    assert HOSTED.JOB_FREE_DISK_FLOOR_BYTES == floors[0]
    for family in ("addresses", "places"):
        cap = HOSTED.HOSTED_LIMITS[family]["max_scratch_bytes"]
        assert cap == HOSTED.HOSTED_MAX_SCRATCH_BYTES, family
        assert cap < HOSTED.JOB_FREE_DISK_FLOOR_BYTES, family
        # And with real headroom: the frozen spec declares a minimum headroom
        # fraction, and a cap one byte under the floor would satisfy `<` while still
        # leaving nothing for the filesystem's own overhead.
        headroom = json.loads(PLACES_SPEC.read_text())["acceptance_gates"]["resources"][
            "resource_headroom_min_fraction"
        ]
        assert cap <= HOSTED.JOB_FREE_DISK_FLOOR_BYTES * (1 - headroom), family


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
        # The ADMITTED remote-operation cap, read from the control module rather
        # than restated: predict-reduce now projects the finalize publication
        # against it on the real planet inventories, and a stale literal here would
        # be testing a budget no run is dispatched with.
        "caps": {"max_reducers_per_family": max_reducers,
                 "max_remote_operations": CONTROL.CAPS["max_remote_operations"],
                 "max_remote_write_bytes": 1_000_000_000_000},
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


def test_predict_reduce_addresses_models_the_per_country_bisection(tmp_path, capsys):
    # The addresses branch divided total records by a row cap and nothing else --
    # the same defect PR #155 fixed on the places branch. The address planner
    # bisects each country independently, so every country contributes at least one
    # partition and an over-cap country's leaf count is a power of two. On the
    # planet inventory the total-row figure is 474 and the modelled shape is 725,
    # so the gate was ~1.5x optimistic (not 14x: the address per-country term is
    # 34 countries, far below the total-row figure, unlike Places' 16,633 cells).
    contract = _contract(tmp_path)
    assert HOSTED.main([
        "predict-reduce", "--contract", str(contract), "--family", "addresses",
        "--inventory", str(ADDRESS_INVENTORY),
    ]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    inventory = json.loads(ADDRESS_INVENTORY.read_text())
    row_cap = HOSTED.HOSTED_LIMITS["addresses"]["max_pack_rows"]
    by_rows = -(-HOSTED._inventory_total_records(inventory) // row_cap)
    modelled, _basis = HOSTED._address_uniform_partition_estimate(inventory, row_cap)
    assert by_rows == 474 and modelled == 725
    assert out["predicted_partitions"] == modelled > by_rows
    assert "per-country uniform hash-bisection estimate" in out["prediction_basis"]


def test_address_estimate_counts_every_country_and_rounds_to_powers_of_two():
    # Both effects the total-row division misses, isolated. Two countries of one row
    # each are two partitions, not one; a country at 3x the cap is modelled at four
    # leaves, not three, because the split halves.
    #
    # This is an ESTIMATE and not a bound: with even spreading the real planner
    # stops as soon as a subtree fits and can land BELOW the model, and under
    # in-country bucket skew it can land above. See the function's docstring; it is
    # well conditioned here only because route_hash is uniform by construction.
    modelled, basis = HOSTED._address_uniform_partition_estimate(
        {"exact_country_rows": {"AA": 1, "BB": 1}, "totals": {"records": 2}}, 1_000_000
    )
    assert modelled == 2 and "2 inventory countries" in basis
    modelled, _basis = HOSTED._address_uniform_partition_estimate(
        {"exact_country_rows": {"AA": 3_000_000}, "totals": {"records": 3_000_000}},
        1_000_000,
    )
    assert modelled == 4


def _real_address_partitions(per_bucket: dict[int, int], row_cap: int) -> int:
    """Partition count the REAL address planner produces for one country."""
    address = HOSTED.ADDRESS

    def binding(records: int) -> dict:
        return {"records": records, "semantic_sum_a": f"{records:064x}",
                "semantic_sum_b": f"{records * 3:064x}"}

    summaries = {("AA", bucket): binding(rows)
                 for bucket, rows in per_bucket.items() if rows}
    expected = address.combine_bindings(list(summaries.values()))
    plan = address._plan_from_summaries(summaries, expected, row_cap)
    return len(plan["partitions"])


def test_address_estimate_is_not_a_bound_in_either_direction():
    # Pinned against the REAL planner so the docstring's honesty claim cannot rot.
    # Even spreading: the planner stops bisecting a subtree as soon as that subtree
    # fits, so the model OVERSHOOTS.
    even = {bucket * (1 << 16) // 8: 500_000 + (3 if bucket == 7 else 0)
            for bucket in range(8)}
    assert _real_address_partitions(even, 1_000_000) == 5
    assert HOSTED._address_uniform_partition_estimate(
        {"exact_country_rows": {"AA": sum(even.values())},
         "totals": {"records": sum(even.values())}}, 1_000_000)[0] == 8
    # In-country bucket skew: every split isolates a light sibling that is already
    # under the cap and each becomes its own leaf, so the model UNDERSHOOTS.
    skewed = {0: 950_000, 1: 950_000}
    skewed.update({bucket * (1 << 16) // 16: 10_000 for bucket in range(1, 11)})
    assert _real_address_partitions(skewed, 1_000_000) == 6
    assert HOSTED._address_uniform_partition_estimate(
        {"exact_country_rows": {"AA": sum(skewed.values())},
         "totals": {"records": sum(skewed.values())}}, 1_000_000)[0] == 2


def test_address_estimate_fails_closed_without_per_country_rows():
    # Falling back to the total-row figure would silently restore the optimism.
    with pytest.raises(SystemExit, match="exact_country_rows"):
        HOSTED._address_uniform_partition_estimate({"totals": {"records": 10}}, 1_000_000)


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


class _RecordingStore:
    """Records marker writes so a test can prove none were published."""

    def __init__(self, root: Path):
        self.root = root
        self.markers: list[str] = []

    def write_marker_last(self, key: str, value: dict) -> None:
        self.markers.append(key)


def _reduce_plan(tmp_path: Path, *, partition_count: int) -> Path:
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({
        "partitions": [],
        "reduce_execution": {
            "ownership": "shuffle-bucket-range",
            "batches": [{"batch_index": 0, "bucket_start": 0, "bucket_end": 3,
                         "partition_start": 0, "partition_count": partition_count}],
        },
    }) + "\n")
    (tmp_path / "markers").mkdir(exist_ok=True)
    (tmp_path / "markers" / "places-map-000.json").write_text("{}\n")
    return plan


def _run_reduce_batch_zero(tmp_path: Path) -> int:
    return HOSTED.main([
        "run-reduce", "--contract", str(_contract(tmp_path)), "--family", "places",
        "--store-root", str(tmp_path / "store"), "--plan", str(tmp_path / "plan.json"),
        "--markers-dir", str(tmp_path / "markers"), "--batch-index", "0",
        "--encoder-binary", "/nonexistent", "--verifier-binary", "/nonexistent",
        "--scratch-dir", str(tmp_path / "scratch"),
        "--output-dir", str(tmp_path / "reductions"),
    ])


def test_bucket_range_reduce_checks_ownership_before_publishing_any_marker(
    tmp_path, monkeypatch
):
    # A completion marker is a create-only OWNERSHIP CLAIM: published into the
    # run-scoped staging prefix it can never be replaced, so a marker written for a
    # partition this job does not own leaves the run unresumable. The plan-agreement
    # check therefore has to run BEFORE the marker loop -- it used to run after it,
    # in the caller, which meant an ownership disagreement first surfaced as an
    # opaque "existing completion marker differs" from the colliding marker, or,
    # with no collision, published the wrong claim and only then aborted.
    reduction = {
        "partition": {"id": "p-0000"},
        "routed_object": {"key": "serve/places-v1/routed/sha256/" + "0" * 64 + ".plrv",
                          "bytes": 1, "sha256": "0" * 64},
    }
    monkeypatch.setattr(
        HOSTED.PLACES,
        "reduce_bucket_range",
        lambda **_kwargs: {
            "bucket_start": 0, "bucket_end": 3, "fragments_opened": 1,
            # The reducer claims partitions 7 and 8; the plan assigned 0 and 1.
            "partition_indexes": [7, 8],
            "reductions": [reduction, reduction],
        },
    )
    store = _RecordingStore(tmp_path / "store")
    monkeypatch.setattr(HOSTED, "_store", lambda *_args, **_kwargs: store)
    _reduce_plan(tmp_path, partition_count=2)
    with pytest.raises(SystemExit) as excinfo:
        _run_reduce_batch_zero(tmp_path)
    assert "disagree about ownership" in str(excinfo.value)
    assert store.markers == []
    # And no reduction JSON either: finalize globs that directory, so a file for an
    # unowned partition would be read back as a real reduction.
    assert not sorted((tmp_path / "reductions").glob("*.json"))


def test_bucket_range_reduce_publishes_when_the_plan_agrees(tmp_path, monkeypatch):
    # The same path with agreeing ownership still writes one marker and one
    # reduction per partition, so the check above is a guard and not a blanket stop.
    def _reduction(index: int) -> dict:
        digest = f"{index:064x}"
        return {"partition": {"id": f"p-{index:04d}"},
                "routed_object": {"key": f"serve/places-v1/routed/sha256/{digest}.plrv",
                                  "bytes": 1, "sha256": digest}}

    monkeypatch.setattr(
        HOSTED.PLACES,
        "reduce_bucket_range",
        lambda **_kwargs: {
            "bucket_start": 0, "bucket_end": 3, "fragments_opened": 2,
            "partition_indexes": [0, 1],
            "reductions": [_reduction(0), _reduction(1)],
        },
    )
    store = _RecordingStore(tmp_path / "store")
    monkeypatch.setattr(HOSTED, "_store", lambda *_args, **_kwargs: store)
    _reduce_plan(tmp_path, partition_count=2)
    assert _run_reduce_batch_zero(tmp_path) == 0
    assert store.markers == [
        HOSTED._reduce_marker_key("places", 0),
        HOSTED._reduce_marker_key("places", 1),
    ]
    assert [p.name for p in sorted((tmp_path / "reductions").glob("*.json"))] == [
        "0000.json", "0001.json",
    ]


def test_create_only_marker_conflict_names_the_key_and_both_payloads(tmp_path):
    # The guard is correct and must stay; what it says is the problem. Diagnosing a
    # real conflict needed the KEY and both payloads, and the bare message carried
    # neither -- the two causes (a store reused across producer revisions vs two
    # jobs claiming one slot) are told apart only by what the payloads say.
    store = HOSTED.ADDRESS.LocalObjectStore(tmp_path / "store")
    key = HOSTED._reduce_marker_key("places", 0)
    store.write_marker_last(key, {"partition_index": 0, "artifact": None})
    # A byte-identical rewrite stays a no-op: that is what makes a retried job safe.
    store.write_marker_last(key, {"partition_index": 0, "artifact": None})
    with pytest.raises(ValueError) as excinfo:
        store.write_marker_last(
            key, {"partition_index": 0, "artifact": {"sha256": "a" * 64}}
        )
    message = str(excinfo.value)
    assert message.startswith("existing completion marker differs")
    assert key in message
    # Both payloads are identified, and the existing one is quoted, so the reader
    # can see WHICH field moved without re-running anything.
    assert message.count("sha256=") == 2
    assert '"artifact":null' in message


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
