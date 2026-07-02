"""Tests for build_id_index.py pipeline logic (no R2 access required).

Covers the pure sub-range planning, the transient-retry wrapper, and the
marker semantics of the main pipeline (patch runs must never read or write
run-level markers; explicit metadata runs must regenerate).
"""

import sys
from argparse import Namespace
from pathlib import Path
from unittest import mock

import duckdb
import pytest

# Add scripts to path
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import build_id_index as bii


# ---------------------------------------------------------------------------
# _registry_sub_ranges
# ---------------------------------------------------------------------------

def covered_ints(sub_ranges, prefix_len):
    """Reconstruct the set of prefix ints covered by range sub-ranges."""
    covered = set()
    for _, _, clear_kwargs in sub_ranges:
        lo = int(clear_kwargs["prefix_start"], 16)
        hi = int(clear_kwargs["prefix_end"], 16)
        covered.update(range(lo, hi + 1))
    return covered


def test_sub_ranges_full_run_tiles_entire_prefix_space():
    sub_ranges, label = bii._registry_sub_ranges(3)
    assert len(sub_ranges) == 4096 // bii.SUB_RANGE_PARTITIONS
    assert covered_ints(sub_ranges, 3) == set(range(4096))
    assert "000-fff" in label


def test_sub_ranges_range_tiles_exactly():
    sub_ranges, _ = bii._registry_sub_ranges(3, prefix_start="800", prefix_end="bff")
    assert len(sub_ranges) == 1024 // bii.SUB_RANGE_PARTITIONS
    assert covered_ints(sub_ranges, 3) == set(range(0x800, 0xC00))
    # No overlap between consecutive sub-ranges
    bounds = [(c["prefix_start"], c["prefix_end"]) for _, _, c in sub_ranges]
    for (_, hi), (lo, _) in zip(bounds, bounds[1:]):
        assert int(lo, 16) == int(hi, 16) + 1


def test_sub_ranges_last_global_range_has_no_upper_id_bound():
    sub_ranges, _ = bii._registry_sub_ranges(3, prefix_start="f80", prefix_end="fff")
    cond = sub_ranges[-1][0]
    assert "id >= 'f80'" in cond
    assert "id < " not in cond  # fff is the last prefix; no upper raw-id bound


def test_sub_ranges_bound_the_partition_key():
    # The raw-id bounds assume lowercase hex; the partition-key BETWEEN keeps
    # a non-lowercase ID from being staged into a parallel job's partition.
    sub_ranges, _ = bii._registry_sub_ranges(3, prefix_start="800", prefix_end="bff")
    for cond, _, clear_kwargs in sub_ranges:
        assert (f"lower(left(replace(id, '-', ''), 3))"
                f" BETWEEN '{clear_kwargs['prefix_start']}'"
                f" AND '{clear_kwargs['prefix_end']}'") in cond


def test_sub_ranges_explicit_prefixes():
    sub_ranges, label = bii._registry_sub_ranges(3, prefixes=["001", "4a2"])
    assert len(sub_ranges) == 1
    cond, _, clear_kwargs = sub_ranges[0]
    assert "id LIKE '001%'" in cond and "id LIKE '4a2%'" in cond
    # Bounding range for zone-map pushdown (OR-of-LIKEs alone may not push)
    assert "id >= '001'" in cond and "id < '4a3'" in cond
    assert clear_kwargs == {"prefixes": ["001", "4a2"]}
    assert "2 explicit prefixes" in label


def test_sub_ranges_explicit_prefixes_at_end_have_no_upper_bound():
    sub_ranges, _ = bii._registry_sub_ranges(3, prefixes=["ffe", "fff"])
    cond = sub_ranges[0][0]
    assert "id >= 'ffe'" in cond
    assert "id < " not in cond  # fff is the last prefix; no upper raw-id bound


# ---------------------------------------------------------------------------
# Bucketed release staging
# ---------------------------------------------------------------------------

def test_release_query_includes_bucket_partition_key():
    q = bii._release_id_query_for_type(3, "2026-01-01.0", "addresses", "address")
    assert "lower(left(replace(id, '-', ''), 1)) as bucket" in q
    assert "lower(left(replace(id, '-', ''), 3)) as prefix" in q


BUCKETED = [
    "s3://b/v/staging/id-release-addresses-address/bucket=0/data_0.parquet",
    "s3://b/v/staging/id-release-addresses-address/bucket=3/data_0.parquet",
    "s3://b/v/staging/id-release-addresses-address/bucket=7/data_0.parquet",
    "s3://b/v/staging/id-release-base-water/bucket=0/data_0.parquet",
]
LEGACY = ["s3://b/v/staging/id-release-base-land/data.parquet"]


def test_release_files_filtered_to_range_buckets():
    # A 000-3ff range job needs buckets 0..3 only
    prefixes = [format(i, '03x') for i in range(0x000, 0x400)]
    kept = bii._release_files_for_prefixes(BUCKETED + LEGACY, prefixes)
    assert [f for f in kept if "/bucket=" in f] == BUCKETED[:2] + [BUCKETED[3]]
    # Legacy single-file staging carries all buckets: always kept
    assert LEGACY[0] in kept


def test_release_files_explicit_prefixes_pick_their_buckets():
    kept = bii._release_files_for_prefixes(BUCKETED + LEGACY, ["7a2"])
    assert [f for f in kept if "/bucket=" in f] == [BUCKETED[2]]
    assert LEGACY[0] in kept


def test_release_files_no_prefixes_keeps_everything():
    assert bii._release_files_for_prefixes(BUCKETED + LEGACY, None) == BUCKETED + LEGACY


# ---------------------------------------------------------------------------
# _retry_transient
# ---------------------------------------------------------------------------

def test_retry_transient_runs_on_retry_between_attempts():
    calls = []
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise duckdb.IOException("connection reset")
        return "ok"

    result = bii._retry_transient(
        flaky, backoff=0, on_retry=lambda: calls.append(1))()
    assert result == "ok"
    assert len(attempts) == 3
    assert len(calls) == 2  # before each retry, not before the first attempt


def test_retry_transient_does_not_retry_permanent_errors():
    calls = []

    def broken():
        raise ValueError("permanent")

    with pytest.raises(ValueError):
        bii._retry_transient(broken, backoff=0, on_retry=lambda: calls.append(1))()
    assert calls == []


def test_retry_transient_no_files_found_is_not_transient():
    def absent():
        raise duckdb.IOException("No files found that match the pattern")

    with pytest.raises(duckdb.IOException):
        bii._retry_transient(absent, backoff=0)()


def test_retry_transient_disk_full_is_not_transient():
    attempts = []

    def full():
        attempts.append(1)
        raise duckdb.IOException(
            'IO Error: Could not write file ".tmp/x.tmp": No space left on device')

    with pytest.raises(duckdb.IOException):
        bii._retry_transient(full, backoff=0)()
    assert len(attempts) == 1  # fail fast, no pointless retries


# ---------------------------------------------------------------------------
# Pipeline marker semantics
# ---------------------------------------------------------------------------

def make_args(**overrides):
    args = Namespace(
        version="2026-01-01.0",
        version_suffix="0",
        release="2026-01-01.0",
        prefix_len=3,
        dry_run=False,
        smoke_test=False,
        workers=1,
        r2_account_id="acct",
        r2_access_key="key",
        r2_secret_key="secret",
        r2_bucket="geocoder-shards",
        phase=None,
        prefix_start=None,
        prefix_end=None,
        prefixes=None,
        marker_ranges=None,
    )
    for k, v in overrides.items():
        setattr(args, k, v)
    return args


@pytest.fixture
def pipeline(monkeypatch):
    """Run build_id_index with all R2-touching functions mocked.

    Returns (run, mocks): run(args) executes the pipeline; mocks holds the
    mock for each patched function.
    """
    mocks = {}
    for name, ret in [
        ("_read_staging_marker", None),
        ("_write_staging_marker", None),
        ("phase_partition_r2", None),
        ("phase_partition_release_r2", None),
        ("phase_build_r2", [("001", 10, 360, None)]),
        ("phase_metadata", ({"001": {"record_count": 10}}, 10, [])),
        ("_gather_shard_info_from_r2", [("001", None, 0, None)]),
    ]:
        m = mock.Mock(return_value=ret)
        monkeypatch.setattr(bii, name, m)
        mocks[name] = m
    return bii.build_id_index, mocks


def read_marker_keys(mocks):
    return [c.args[2] for c in mocks["_read_staging_marker"].call_args_list]


def written_marker_keys(mocks):
    return [c.args[2] for c in mocks["_write_staging_marker"].call_args_list]


def test_patch_stage_never_touches_run_level_marker(pipeline):
    run, mocks = pipeline
    run(make_args(phase="stage-registry", prefixes="001,4a2"))

    # Must not read the suffix-less full-run marker (it would skip the patch)
    assert "id-partitioned" not in read_marker_keys(mocks)
    # Must not write any marker (only a subset was re-staged)
    assert written_marker_keys(mocks) == []
    mocks["phase_partition_r2"].assert_called_once()
    assert mocks["phase_partition_r2"].call_args.kwargs["prefixes"] == ["001", "4a2"]


def test_patch_stage_with_marker_ranges_writes_only_those(pipeline):
    run, mocks = pipeline
    run(make_args(phase="stage-registry", prefixes="001",
                  marker_ranges="000-3ff,c00-fff"))

    assert written_marker_keys(mocks) == [
        "id-partitioned-000-3ff", "id-partitioned-c00-fff"]
    assert "id-partitioned" not in read_marker_keys(mocks)


def test_range_stage_skips_when_range_marker_exists(pipeline):
    run, mocks = pipeline
    mocks["_read_staging_marker"].return_value = {"status": "complete"}
    run(make_args(phase="stage-registry", prefix_start="000", prefix_end="3ff"))

    assert "id-partitioned-000-3ff" in read_marker_keys(mocks)
    mocks["phase_partition_r2"].assert_not_called()


def test_range_stage_writes_range_marker(pipeline):
    run, mocks = pipeline
    run(make_args(phase="stage-registry", prefix_start="000", prefix_end="3ff"))

    mocks["phase_partition_r2"].assert_called_once()
    assert written_marker_keys(mocks) == ["id-partitioned-000-3ff"]


def test_patch_build_bypasses_markers_and_rebuilds(pipeline):
    run, mocks = pipeline
    # Even with a stale bare "build" marker present, a patch build must run
    mocks["_read_staging_marker"].return_value = {"status": "complete"}
    run(make_args(phase="build", prefixes="001"))

    assert "build" not in read_marker_keys(mocks)
    mocks["phase_build_r2"].assert_called_once()
    assert mocks["phase_build_r2"].call_args.kwargs["prefixes"] == ["001"]
    # No bare "build" marker: it would block future patches and be
    # double-counted by _sum_build_marker_records' build*/_SUCCESS glob
    assert written_marker_keys(mocks) == []


def test_range_build_writes_range_marker_with_records(pipeline):
    run, mocks = pipeline
    run(make_args(phase="build", prefix_start="000", prefix_end="3ff"))

    mocks["phase_build_r2"].assert_called_once()
    assert written_marker_keys(mocks) == ["build-000-3ff"]
    extra = mocks["_write_staging_marker"].call_args.kwargs["extra"]
    assert extra == {"records": 10}


def test_explicit_metadata_regenerates_despite_marker(pipeline):
    run, mocks = pipeline
    mocks["_read_staging_marker"].return_value = {"status": "complete"}
    run(make_args(phase="metadata"))

    assert "metadata" not in read_marker_keys(mocks)
    mocks["phase_metadata"].assert_called_once()
    assert "metadata" in written_marker_keys(mocks)


def test_full_run_resume_skips_completed_phases(pipeline):
    run, mocks = pipeline
    mocks["_read_staging_marker"].return_value = {"status": "complete"}
    run(make_args(phase=None))  # "all"

    mocks["phase_partition_r2"].assert_not_called()
    mocks["phase_partition_release_r2"].assert_not_called()
    mocks["phase_build_r2"].assert_not_called()
    mocks["phase_metadata"].assert_not_called()


def test_full_run_executes_all_phases_and_markers(pipeline):
    run, mocks = pipeline
    run(make_args(phase=None))  # "all", no markers exist

    mocks["phase_partition_r2"].assert_called_once()
    mocks["phase_partition_release_r2"].assert_called_once()
    mocks["phase_build_r2"].assert_called_once()
    mocks["phase_metadata"].assert_called_once()
    # ("id-release" is written inside phase_partition_release_r2, mocked here)
    assert written_marker_keys(mocks) == ["id-partitioned", "build", "metadata"]
