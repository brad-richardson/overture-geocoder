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
import patch_failed_shards as pfs


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


def test_registry_query_preserves_path_semantics():
    q = bii._registry_id_query(3, "id >= '000'")
    assert "regexp_extract(path, '(^|/)type=([^/]+)/', 2)" in q
    assert "regexp_extract(path, '([^/]+)$', 1)" in q
    assert "last_seen::VARCHAR as last_seen_release" in q
    assert "true::BOOLEAN as registry_member" in q
    assert "regexp_extract(path, '(^|/)theme=([^/]+)/', 2)" in q


@pytest.mark.parametrize("path", [
    "theme=places/type=place/part-00001.zstd.parquet",
    "/theme=places/type=place/part-00001.zstd.parquet",
])
def test_registry_path_extraction_accepts_relative_or_slash_prefixed(path):
    con = duckdb.connect()
    row = con.execute("""
        SELECT
            NULLIF(regexp_extract(?, '(^|/)theme=([^/]+)/', 2), ''),
            NULLIF(regexp_extract(?, '(^|/)type=([^/]+)/', 2), ''),
            NULLIF(regexp_extract(?, '([^/]+)$', 1), '')
    """, [path, path, path]).fetchone()
    assert row == ("places", "place", "part-00001.zstd.parquet")


def test_release_query_captures_filename_and_known_locator_fields():
    q = bii._release_id_query_for_type(
        3, "2026-06-17.0", "addresses", "address")
    assert "filename=true" in q
    assert "'address'::VARCHAR as feature_type" in q
    assert "regexp_extract(filename, '([^/]+)$', 1)" in q
    assert "'2026-06-17.0'::VARCHAR as last_seen_release" in q
    assert "false::BOOLEAN as registry_member" in q
    assert "'addresses'::VARCHAR as source_theme" in q


def test_expected_schema_keeps_v1_columns_first():
    assert [name for name, _ in bii.EXPECTED_SHARD_COLUMNS] == [
        "id", "bbox_xmin", "bbox_ymin", "bbox_xmax", "bbox_ymax",
        "feature_type", "filename", "last_seen_release", "registry_member",
    ]


def test_locator_fixture_is_dictionary_encoded_and_bounded(tmp_path):
    output = tmp_path / "locator-v2.parquet"
    con = duckdb.connect()
    con.execute(f"""
        COPY (
            SELECT md5(i::VARCHAR)::UUID AS id,
                   1::FLOAT AS bbox_xmin, 2::FLOAT AS bbox_ymin,
                   3::FLOAT AS bbox_xmax, 4::FLOAT AS bbox_ymax,
                   CASE WHEN i % 2 = 0 THEN 'address' ELSE 'water' END::VARCHAR
                       AS feature_type,
                   CASE WHEN i % 2 = 0 THEN 'part-address.zstd.parquet'
                        ELSE 'part-water.zstd.parquet' END::VARCHAR AS filename,
                   '2026-06-17.0'::VARCHAR AS last_seen_release,
                   (i % 2 = 1)::BOOLEAN AS registry_member
            FROM range(10000) AS rows(i)
            ORDER BY id
        ) TO '{output}'
        (FORMAT PARQUET, COMPRESSION UNCOMPRESSED, ROW_GROUP_SIZE 2048)
    """)
    bii._assert_shard_schema(con, str(output))
    encodings = con.execute(f"""
        SELECT DISTINCT encodings
        FROM parquet_metadata('{output}')
        WHERE path_in_schema IN ('feature_type', 'filename', 'last_seen_release')
    """).fetchall()
    assert any("DICTIONARY" in str(row[0]) for row in encodings)
    assert output.stat().st_size < 600_000

    legacy = tmp_path / "locator-v1.parquet"
    con.execute(f"""
        COPY (
            SELECT md5(i::VARCHAR)::UUID AS id,
                   1::FLOAT AS bbox_xmin, 2::FLOAT AS bbox_ymin,
                   3::FLOAT AS bbox_xmax, 4::FLOAT AS bbox_ymax
            FROM range(10) AS rows(i) ORDER BY id
        ) TO '{legacy}' (FORMAT PARQUET, COMPRESSION UNCOMPRESSED)
    """)
    assert bii._classify_shard_set(con, [str(legacy)]) == 1
    assert bii._classify_shard_set(con, [str(output)]) == 2
    with pytest.raises(RuntimeError, match="Mixed ID shard formats"):
        bii._classify_shard_set(con, [str(legacy), str(output)])


def test_locator_audit_rejects_duplicate_ids():
    con = duckdb.connect()
    query = """
        SELECT '00000000-0000-0000-0000-000000000001'::UUID AS id,
               'address'::VARCHAR AS feature_type,
               'part.parquet'::VARCHAR AS filename,
               '2026-06-17.0'::VARCHAR AS last_seen_release,
               'addresses'::VARCHAR AS source_theme
        UNION ALL
        SELECT '00000000-0000-0000-0000-000000000001'::UUID,
               'address', 'part.parquet', '2026-06-17.0', 'addresses'
    """
    with pytest.raises(RuntimeError, match="Duplicate ID"):
        bii._assert_locator_rows(con, query, "000", "2026-06-17.0")


@pytest.mark.parametrize("feature_type,last_seen", [
    ("future_type", "2026-06-17.0"),
    ("address", "2026-05-20.0"),
])
def test_locator_audit_rejects_unknown_type_or_stale_filename(
        feature_type, last_seen):
    con = duckdb.connect()
    query = f"""
        SELECT '00000000-0000-0000-0000-000000000001'::UUID AS id,
               '{feature_type}'::VARCHAR AS feature_type,
               'part.parquet'::VARCHAR AS filename,
               '{last_seen}'::VARCHAR AS last_seen_release,
               'addresses'::VARCHAR AS source_theme
    """
    with pytest.raises(RuntimeError, match="Invalid locator metadata"):
        bii._assert_locator_rows(con, query, "000", "2026-06-17.0")


@pytest.mark.parametrize("filename,source_theme", [
    ("nested/part.parquet", "addresses"),
    (r"nested\part.parquet", "addresses"),
    ("part.txt", "addresses"),
    ("part.parquet", "places"),
])
def test_locator_audit_rejects_bad_basename_or_theme(filename, source_theme):
    con = duckdb.connect()
    query = """
        SELECT '00000000-0000-0000-0000-000000000001'::UUID AS id,
               'address'::VARCHAR AS feature_type,
               ?::VARCHAR AS filename,
               '2026-06-17.0'::VARCHAR AS last_seen_release,
               ?::VARCHAR AS source_theme
    """
    con.execute("CREATE TEMP TABLE invalid_locator AS " + query,
                [filename, source_theme])
    with pytest.raises(RuntimeError, match="Invalid locator metadata"):
        bii._assert_locator_rows(
            con, "SELECT * FROM invalid_locator", "000", "2026-06-17.0")


def test_release_theme_type_mismatch_fails_before_remote_access():
    with pytest.raises(RuntimeError, match="Unsupported release theme/type"):
        bii._partition_release_type(
            "base", "address", 3, "2026-06-17.0", {}, "v")


def test_patch_release_discovery_distinguishes_empty_from_error(monkeypatch):
    monkeypatch.setattr(pfs, "_glob_files", lambda *_: [])
    with pytest.raises(RuntimeError, match="No release staging files"):
        pfs._discover_release_staging_files(object(), "bucket", "v")

    failure = duckdb.IOException("authentication failed")
    monkeypatch.setattr(
        pfs, "_glob_files", mock.Mock(side_effect=failure))
    with pytest.raises(duckdb.IOException, match="authentication failed"):
        pfs._discover_release_staging_files(object(), "bucket", "v")


def test_patch_bucketed_release_downloads_use_unique_temp_paths():
    remote_files = [
        "s3://b/v/staging/id-release-addresses-address/bucket=0/data_0.parquet",
        "s3://b/v/staging/id-release-addresses-address/bucket=1/data_0.parquet",
    ]
    paths = [
        pfs._local_release_path(remote, index, pid=123)
        for index, remote in enumerate(remote_files)
    ]
    assert len(set(paths)) == len(remote_files)
    assert all("id-release-addresses-address" in path for path in paths)


def test_gen_collection_metadata_never_upgrades_v1_schema():
    assert bii._format_metadata(1, "2026-06-17.0") == {}
    metadata = bii._format_metadata(2, "2026-06-17.0")
    assert metadata["format_version"] == 2
    assert metadata["overture_release"] == "2026-06-17.0"
    assert metadata["type_theme_map"]["types"]["address"] == "addresses"


def test_phase_metadata_emits_legacy_fields_for_uniform_v1(monkeypatch):
    uploaded = {}

    def capture(path, key):
        uploaded[key] = __import__("json").loads(path.read_text())
        return None

    monkeypatch.setattr(bii, "_detect_output_shard_format", lambda *_: 1)
    monkeypatch.setattr(bii, "_upload_to_r2", capture)
    bii.phase_metadata(
        [("000", 1, 32, None)], 3, "v", "2026-06-17.0",
        {"bucket": "test"},
    )

    meta = uploaded["test/v/id-meta.json"]
    collection = uploaded["test/v/id-collection.json"]
    assert "format_version" not in meta
    assert "type_theme_map" not in meta
    assert "format_version" not in collection["summaries"]
    assert collection["summaries"]["overture_release"] == "2026-06-17.0"


def test_phase_metadata_schema_failure_precedes_upload(monkeypatch):
    upload = mock.Mock()
    monkeypatch.setattr(
        bii, "_detect_output_shard_format",
        mock.Mock(side_effect=RuntimeError("Mixed ID shard formats")),
    )
    monkeypatch.setattr(bii, "_upload_to_r2", upload)
    with pytest.raises(RuntimeError, match="Mixed ID shard formats"):
        bii.phase_metadata(
            [("000", 1, 32, None)], 3, "v", "2026-06-17.0",
            {"bucket": "test"},
        )
    upload.assert_not_called()


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
        ("phase_metadata", ({"001": {"record_count": 10}}, 10, [], 2)),
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
    mocks["_read_staging_marker"].return_value = {
        "status": "complete", "format_version": 2}
    run(make_args(phase="stage-registry", prefix_start="000", prefix_end="3ff"))

    assert "id-partitioned-000-3ff" in read_marker_keys(mocks)
    mocks["phase_partition_r2"].assert_not_called()


def test_range_stage_writes_range_marker(pipeline):
    run, mocks = pipeline
    run(make_args(phase="stage-registry", prefix_start="000", prefix_end="3ff"))

    mocks["phase_partition_r2"].assert_called_once()
    assert written_marker_keys(mocks) == ["id-partitioned-000-3ff"]


def test_v1_marker_never_skips_v2_stage_or_build(pipeline):
    run, mocks = pipeline
    mocks["_read_staging_marker"].return_value = {"status": "complete"}

    run(make_args(phase="stage-registry", prefix_start="000", prefix_end="3ff"))
    mocks["phase_partition_r2"].assert_called_once()

    mocks["phase_build_r2"].reset_mock()
    run(make_args(phase="build", prefix_start="000", prefix_end="3ff"))
    mocks["phase_build_r2"].assert_called_once()


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
    mocks["_read_staging_marker"].return_value = {
        "status": "complete", "format_version": 2}
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
