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
import gen_id_collection as gic
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


def test_smoke_release_query_filters_prefixes_before_limit():
    q = bii._release_id_query_for_type(
        3,
        "2026-01-01.0",
        "addresses",
        "address",
        limit=50,
        prefixes=["004", "000", "004"],
    )
    prefix_filter = (
        "AND lower(left(replace(id, '-', ''), 3)) IN ('000', '004')"
    )
    assert prefix_filter in q
    assert q.index(prefix_filter) < q.index("LIMIT 50")


@pytest.mark.parametrize("prefixes", [[], ["00"], ["00g"]])
def test_release_query_rejects_invalid_prefix_filter(prefixes):
    with pytest.raises(ValueError, match="prefix"):
        bii._release_id_query_for_type(
            3,
            "2026-01-01.0",
            "addresses",
            "address",
            limit=50,
            prefixes=prefixes,
        )


def test_registry_query_preserves_path_semantics():
    q = bii._registry_id_query(3, "id >= '000'")
    assert "regexp_extract(path, '(^|/)type=([^/]+)/', 2)" in q
    assert "regexp_extract(path, '([^/]+)$', 1)" in q
    assert "last_seen::VARCHAR as last_seen_release" in q
    assert "true::BOOLEAN as registry_member" in q
    assert "regexp_extract(path, '(^|/)theme=([^/]+)/', 2)" in q


def test_smoke_historical_registry_query_is_explicitly_historical():
    q = bii._smoke_historical_registry_query()
    assert f"'{bii.SMOKE_HISTORICAL_ID}'::UUID as id" in q
    assert "NULL::VARCHAR as filename" in q
    assert (
        f"'{bii.SMOKE_HISTORICAL_RELEASE}'::VARCHAR as last_seen_release" in q
    )
    assert "true::BOOLEAN as registry_member" in q


def test_smoke_historical_registry_row_uses_its_hive_prefix(tmp_path):
    destination = tmp_path / "staging"
    (destination / "prefix=000").mkdir(parents=True)
    con = duckdb.connect()
    try:
        bii._write_smoke_historical_registry_row(con, str(destination), 3)
        row = con.execute(
            """
            SELECT CAST(id AS VARCHAR), filename, last_seen_release,
                   registry_member, prefix
            FROM read_parquet(?, hive_partitioning=true)
            """,
            [str(destination / "prefix=*" / "*.parquet")],
        ).fetchone()
    finally:
        con.close()
    assert row == (
        bii.SMOKE_HISTORICAL_ID,
        None,
        bii.SMOKE_HISTORICAL_RELEASE,
        True,
        "000",
    )


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
        "source_file_id", "last_seen_release_id", "registry_member",
    ]


def test_locator_fixture_uses_compact_ids_and_is_bounded(tmp_path):
    output = tmp_path / "locator-v3.parquet"
    con = duckdb.connect()
    con.execute(f"""
        COPY (
            SELECT md5(i::VARCHAR)::UUID AS id,
                   1::FLOAT AS bbox_xmin, 2::FLOAT AS bbox_ymin,
                   3::FLOAT AS bbox_xmax, 4::FLOAT AS bbox_ymax,
                   CASE WHEN i % 2 = 0 THEN 1 ELSE NULL END::INTEGER
                       AS source_file_id,
                   CASE WHEN i % 2 = 1 THEN 1 ELSE NULL END::INTEGER
                       AS last_seen_release_id,
                   (i % 2 = 1)::BOOLEAN AS registry_member
            FROM range(10000) AS rows(i)
            ORDER BY id
        ) TO '{output}'
        (FORMAT PARQUET, COMPRESSION UNCOMPRESSED, ROW_GROUP_SIZE 2048)
    """)
    bii._assert_shard_schema(con, str(output))
    assert output.stat().st_size < 500_000

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
    assert bii._classify_shard_set(con, [str(output)]) == 3
    with pytest.raises(RuntimeError, match="Mixed ID shard formats"):
        bii._classify_shard_set(con, [str(legacy), str(output)])
    bii._assert_shard_locator_footer_stats(con, str(output), 1, 1)


def test_locator_footer_stats_reject_out_of_bounds_and_bad_null_accounting(tmp_path):
    con = duckdb.connect()
    out_of_bounds = tmp_path / "out-of-bounds.parquet"
    con.execute(f"""
        COPY (
          SELECT md5(i::VARCHAR)::UUID id,
                 1::FLOAT bbox_xmin, 2::FLOAT bbox_ymin,
                 3::FLOAT bbox_xmax, 4::FLOAT bbox_ymax,
                 2::INTEGER source_file_id,
                 NULL::INTEGER last_seen_release_id,
                 TRUE::BOOLEAN registry_member
          FROM range(10) values(i)
        ) TO '{out_of_bounds}' (FORMAT PARQUET, COMPRESSION UNCOMPRESSED)
    """)
    with pytest.raises(RuntimeError, match="outside dictionary"):
        bii._assert_shard_locator_footer_stats(
            con, str(out_of_bounds), source_count=1, release_count=1
        )

    both_present = tmp_path / "both-present.parquet"
    con.execute(f"""
        COPY (
          SELECT md5(i::VARCHAR)::UUID id,
                 1::FLOAT bbox_xmin, 2::FLOAT bbox_ymin,
                 3::FLOAT bbox_xmax, 4::FLOAT bbox_ymax,
                 1::INTEGER source_file_id,
                 1::INTEGER last_seen_release_id,
                 TRUE::BOOLEAN registry_member
          FROM range(10) values(i)
        ) TO '{both_present}' (FORMAT PARQUET, COMPRESSION UNCOMPRESSED)
    """)
    with pytest.raises(RuntimeError, match="aggregate locator null-count"):
        bii._assert_shard_locator_footer_stats(
            con, str(both_present), source_count=1, release_count=1
        )

    # Footer aggregates are deliberately only defense-in-depth: these two bad
    # rows cancel in the null totals. The pre-COPY row-level mapping assertion
    # is what rejects this shape in the real builder.
    cancelling = tmp_path / "cancelling-null-counts.parquet"
    con.execute(f"""
        COPY (
          SELECT md5(i::VARCHAR)::UUID id,
                 1::FLOAT bbox_xmin, 2::FLOAT bbox_ymin,
                 3::FLOAT bbox_xmax, 4::FLOAT bbox_ymax,
                 CASE WHEN i = 0 THEN 1 END::INTEGER source_file_id,
                 CASE WHEN i = 0 THEN 1 END::INTEGER last_seen_release_id,
                 TRUE::BOOLEAN registry_member
          FROM range(2) values(i)
        ) TO '{cancelling}' (FORMAT PARQUET, COMPRESSION UNCOMPRESSED)
    """)
    bii._assert_shard_locator_footer_stats(
        con, str(cancelling), source_count=1, release_count=1
    )


@pytest.mark.parametrize("source_count", [700, 1000])
def test_compact_locator_storage_acceptance(tmp_path, source_count):
    """Keep compact v3 within the measured global storage/read budget."""
    con = duckdb.connect()
    base = tmp_path / f"base-{source_count}.parquet"
    compact = tmp_path / f"compact-{source_count}.parquet"
    rows = 500_000
    base_query = f"""
        SELECT md5(i::VARCHAR)::UUID AS id,
               1::FLOAT AS bbox_xmin, 2::FLOAT AS bbox_ymin,
               3::FLOAT AS bbox_xmax, 4::FLOAT AS bbox_ymax
        FROM range({rows}) AS values(i) ORDER BY id
    """
    compact_query = f"""
        SELECT md5(i::VARCHAR)::UUID AS id,
               1::FLOAT AS bbox_xmin, 2::FLOAT AS bbox_ymin,
               3::FLOAT AS bbox_xmax, 4::FLOAT AS bbox_ymax,
               CASE WHEN i % 100 < 6 THEN NULL
                    ELSE ((hash(i) % {source_count}) + 1)::INTEGER END
                   AS source_file_id,
               CASE WHEN i % 100 < 6
                    THEN ((hash(i) % 50) + 1)::INTEGER END
                   AS last_seen_release_id,
               (i % 3 != 0)::BOOLEAN AS registry_member
        FROM range({rows}) AS values(i) ORDER BY id
    """
    for path, query in ((base, base_query), (compact, compact_query)):
        con.execute(f"""
            COPY ({query}) TO '{path}'
            (FORMAT PARQUET, COMPRESSION UNCOMPRESSED, ROW_GROUP_SIZE 50000)
        """)

    delta_per_row = (compact.stat().st_size - base.stat().st_size) / rows
    assert delta_per_row <= 1.9

    def spans(path):
        return con.execute(f"""
            SELECT row_group_id,
                   MIN(LEAST(coalesce(dictionary_page_offset, data_page_offset),
                             data_page_offset)) AS start_off,
                   MAX(LEAST(coalesce(dictionary_page_offset, data_page_offset),
                             data_page_offset) + total_compressed_size) AS end_off
            FROM parquet_metadata('{path}')
            GROUP BY row_group_id ORDER BY row_group_id
        """).fetchall()

    base_spans = spans(base)
    compact_spans = spans(compact)
    range_deltas = [
        (compact_end - compact_start) - (base_end - base_start)
        for (_, base_start, base_end), (_, compact_start, compact_end)
        in zip(base_spans, compact_spans, strict=True)
    ]
    assert max(range_deltas) <= 110 * 1024

    encodings = con.execute(f"""
        SELECT path_in_schema, encodings
        FROM parquet_metadata('{compact}')
        WHERE path_in_schema IN ('source_file_id', 'last_seen_release_id')
    """).fetchall()
    assert encodings
    assert all("DICTIONARY" in str(encoding) for _, encoding in encodings)

    with compact.open("rb") as file:
        file.seek(-8, 2)
        footer_size = int.from_bytes(file.read(4), "little") + 8
    assert footer_size <= 24 * 1024


def test_locator_dictionary_is_deterministic_and_content_addressable():
    release = "2026-06-17.0"
    entries = [
        ("places", "place", "z.parquet"),
        ("addresses", "address", "a.parquet"),
        ("places", "place", "z.parquet"),
    ]
    first = bii._make_locator_dictionary(
        entries, [release, "2026-05-20.0", release], release)
    second = bii._make_locator_dictionary(
        list(reversed(entries)), ["2026-05-20.0", release], release)
    assert first == second
    assert first["source_files"] == [
        {"theme": "addresses", "feature_type": "address", "filename": "a.parquet"},
        {"theme": "places", "feature_type": "place", "filename": "z.parquet"},
    ]
    assert first["last_seen_releases"] == ["2026-05-20.0", release]
    assert first["source_file_id_bounds"] == [1, 2]
    assert first["last_seen_release_id_bounds"] == [1, 2]
    raw = bii._canonical_json_bytes(first)
    assert raw.endswith(b"\n")
    assert len(raw) < 1024 * 1024
    sha256 = bii.hashlib.sha256(raw).hexdigest()
    assert sha256 == bii.hashlib.sha256(
        bii._canonical_json_bytes(second)).hexdigest()
    marker = {
        "dictionary_href": f"id-locator-dictionary-{sha256}.json",
        "dictionary_sha256": sha256,
        "dictionary_size_bytes": len(raw),
    }
    assert bii._locator_dictionary_marker_reference(marker) == (
        marker["dictionary_href"], sha256, len(raw))
    marker["dictionary_size_bytes"] = 0
    with pytest.raises(RuntimeError, match="Invalid locator dictionary marker"):
        bii._locator_dictionary_marker_reference(marker)
    bii._validate_locator_dictionary(first, release)


def test_stage_inventories_are_canonical_scoped_and_content_addressed():
    release = "2026-06-17.0"
    inventory = bii._make_stage_inventory(
        "release_type",
        release,
        {"theme": "addresses", "feature_type": "address"},
        [
            ("addresses", "address", "z.parquet"),
            ("addresses", "address", "a.parquet"),
            ("addresses", "address", "z.parquet"),
        ],
    )
    assert [item["filename"] for item in inventory["source_files"]] == [
        "a.parquet",
        "z.parquet",
    ]
    reference, raw = bii._stage_inventory_reference(inventory)
    assert reference["href"].startswith("./id-inventories/release_type-")
    assert reference["sha256"] == bii.hashlib.sha256(raw).hexdigest()
    assert bii._validate_stage_inventory(inventory, release) == inventory
    assert bii._validate_stage_inventory_reference(reference) == reference


def test_registry_inventory_rejects_source_tuples_and_bad_scope():
    with pytest.raises(RuntimeError, match="scope/values"):
        bii._make_stage_inventory(
            "registry_range",
            "2026-06-17.0",
            {"prefix_start": "000", "prefix_end": "3ff"},
            [("addresses", "address", "a.parquet")],
        )
    with pytest.raises(RuntimeError, match="range"):
        bii._make_stage_inventory(
            "registry_range",
            "2026-06-17.0",
            {"prefix_start": "400", "prefix_end": "3ff"},
            last_seen_releases=["2026-05-20.0"],
        )
    with pytest.raises(RuntimeError, match="prefixes"):
        bii._make_stage_inventory(
            "registry_range",
            "2026-06-17.0",
            {"prefixes": ["001", "000"]},
        )


def test_inventory_references_fail_closed_on_tampering_and_duplicates():
    inventory = bii._make_stage_inventory(
        "registry_range",
        "2026-06-17.0",
        {"prefix_start": "000", "prefix_end": "3ff"},
        last_seen_releases=["2026-05-20.0"],
    )
    reference, _ = bii._stage_inventory_reference(inventory)
    assert len(bii._inventory_set_sha256([reference])) == 64
    with pytest.raises(RuntimeError, match="Duplicate"):
        bii._inventory_set_sha256([reference, reference])
    corrupt = dict(reference, size_bytes=0)
    with pytest.raises(RuntimeError, match="size"):
        bii._validate_stage_inventory_reference(corrupt)
    bad_href = dict(reference, href=reference["href"] + ".other")
    with pytest.raises(RuntimeError, match="href"):
        bii._validate_stage_inventory_reference(bad_href)


@pytest.mark.parametrize(
    ("scopes", "message"),
    [
        ([{"prefix_start": "0", "prefix_end": "7"}], "incomplete"),
        (
            [
                {"prefix_start": "0", "prefix_end": "8"},
                {"prefix_start": "8", "prefix_end": "f"},
            ],
            "Overlapping",
        ),
    ],
)
def test_registry_inventory_fan_in_rejects_missing_and_overlap(
    monkeypatch, scopes, message
):
    class Connection:
        def close(self):
            pass

    marker_paths = [
        f"s3://b/v/staging/id-partitioned-{i}/_SUCCESS" for i in range(len(scopes))
    ]
    monkeypatch.setattr(bii, "_r2_con", lambda *_: Connection())
    monkeypatch.setattr(bii, "_glob_files", lambda *_: marker_paths)

    def load(_config, _version, staging_dir, release, expected_kind):
        index = int(staging_dir.rsplit("-", 1)[-1])
        inventory = bii._make_stage_inventory(
            "registry_range",
            release,
            scopes[index],
            last_seen_releases=["2026-05-20.0"],
        )
        reference, _ = bii._stage_inventory_reference(inventory)
        prefixes = bii._scope_prefixes(scopes[index], 1)
        return {"partitions": len(prefixes)}, reference, inventory

    monkeypatch.setattr(bii, "_load_required_marker_inventory", load)
    with pytest.raises(RuntimeError, match=message):
        bii._load_registry_inventory_fan_in(
            {"bucket": "b"}, "v", "2026-06-17.0", prefix_len=1
        )


def test_required_marker_inventory_rejects_stale_before_payload_read(monkeypatch):
    monkeypatch.setattr(
        bii,
        "_read_staging_marker",
        lambda *_: {"status": "complete", "format_version": 1},
    )
    payload_read = mock.Mock(side_effect=AssertionError("payload read"))
    monkeypatch.setattr(bii, "_load_stage_inventory", payload_read)
    with pytest.raises(RuntimeError, match="missing/stale"):
        bii._load_required_marker_inventory(
            {"bucket": "b"},
            "v",
            "id-partitioned",
            "2026-06-17.0",
            "registry_range",
        )
    payload_read.assert_not_called()


def test_required_staging_marker_upload_failure_is_fatal(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bii, "_upload_to_r2", lambda *_: "upload exhausted")

    with pytest.raises(RuntimeError, match="required staging marker"):
        bii._write_staging_marker(
            {"bucket": "test"}, "v", "build-000-3ff", 1024,
            extra={"dictionary_sha256": "a" * 64},
        )

    assert not list(tmp_path.glob("tmp-staging-marker-*"))


def test_release_inventory_fan_in_rejects_missing_direct_staged_tuple(monkeypatch):
    release = "2026-06-17.0"
    monkeypatch.setattr(
        bii, "_discover_release_types", lambda *_: [("places", "place")]
    )
    inventory = bii._make_stage_inventory(
        "release_type",
        release,
        {"theme": "places", "feature_type": "place"},
        source_files=[("places", "place", "part.parquet")],
    )
    reference, _ = bii._stage_inventory_reference(inventory)
    monkeypatch.setattr(
        bii,
        "_load_required_marker_inventory",
        lambda *_: ({"partitions": 16}, reference, inventory),
    )
    with pytest.raises(RuntimeError, match="file inventory mismatch"):
        bii._load_release_inventory_fan_in(
            {"bucket": "b"},
            "v",
            release,
            [
                ("places", "place", "part.parquet"),
                ("places", "place", "other.parquet"),
            ],
        )


def test_release_source_inventory_requires_two_identical_listings(monkeypatch):
    monkeypatch.setattr(bii, "TYPE_THEME_MAP", {"place": "places"})
    listing = mock.Mock(
        side_effect=[
            [("places", "place", "a.parquet")],
            [("places", "place", "b.parquet")],
            [("places", "place", "b.parquet")],
        ]
    )
    monkeypatch.setattr(bii, "_release_type_source_files", listing)
    monkeypatch.setattr(bii.time, "sleep", lambda *_: None)

    assert bii._discover_current_release_source_files(
        object(), "2026-06-17.0", retries=3
    ) == [("places", "place", "b.parquet")]
    assert listing.call_count == 3


def test_dictionary_inventory_sha_is_validated_and_content_bound():
    stage = bii._make_stage_inventory(
        "registry_range",
        "2026-06-17.0",
        {"prefix_start": "000", "prefix_end": "fff"},
        last_seen_releases=["2026-05-20.0"],
    )
    stage_reference, _ = bii._stage_inventory_reference(stage)
    inventory_set = bii._make_inventory_set([stage_reference], "2026-06-17.0")
    inventory_set_reference, raw = bii._inventory_set_reference(inventory_set)
    sha = inventory_set["inventory_references_sha256"]
    assert inventory_set_reference["size_bytes"] == len(raw)
    assert bii._validate_inventory_set(inventory_set, "2026-06-17.0") == inventory_set
    payload = bii._make_locator_dictionary(
        [("addresses", "address", "a.parquet")],
        ["2026-05-20.0"],
        "2026-06-17.0",
        sha,
        inventory_set_reference,
    )
    assert payload["input_inventory_set_sha256"] == sha
    bii._validate_locator_dictionary(payload, "2026-06-17.0")
    payload["input_inventory_set_sha256"] = "bad"
    with pytest.raises(RuntimeError, match="inventory set SHA"):
        bii._validate_locator_dictionary(payload, "2026-06-17.0")


def _locator_manifest_fixture():
    release = "2026-06-17.0"
    payload = bii._make_locator_dictionary(
        [("places", "place", "part.parquet")],
        ["2026-05-20.0"], release)
    raw = bii._canonical_json_bytes(payload)
    sha256 = bii.hashlib.sha256(raw).hexdigest()
    href = f"id-locator-dictionary-{sha256}.json"
    reference = bii._dictionary_reference(payload, href, sha256, len(raw))
    return ({
        "format_version": 3,
        "overture_release": release,
        "locator_dictionary": reference,
    }, payload, reference)


def test_permanent_manifest_reference_does_not_depend_on_staging_marker(monkeypatch):
    manifest, payload, reference = _locator_manifest_fixture()
    monkeypatch.setattr(bii, "_read_optional_r2_json", lambda *_: manifest)
    monkeypatch.setattr(bii, "_read_r2_json", lambda *_args, **_kwargs: payload)
    staging_read = mock.Mock(side_effect=AssertionError("staging marker consulted"))
    monkeypatch.setattr(bii, "_read_staging_marker", staging_read)

    assert bii._load_locator_dictionary_reference(
        {"bucket": "b"}, "v", "2026-06-17.0") == reference
    assert gic._load_locator_dictionary_reference(
        {"bucket": "b"}, "v", "2026-06-17.0") == reference
    staging_read.assert_not_called()


def test_missing_inventory_binding_requires_new_version():
    with pytest.raises(RuntimeError, match="new version"):
        bii._require_locator_input_inventory_set_sha({})


def test_existing_pre_inventory_manifest_fails_dictionary_phase(monkeypatch):
    monkeypatch.setattr(bii, "_read_optional_r2_json", lambda *_: {})
    monkeypatch.setattr(
        bii, "_load_locator_manifest_and_dictionary",
        lambda *_: ({}, {}, {}),
    )
    with pytest.raises(RuntimeError, match="new version"):
        bii.phase_build_locator_dictionary(
            {"bucket": "b"}, "v", "2026-06-17.0"
        )


def test_build_marker_validation_checks_inventory_binding(monkeypatch):
    monkeypatch.setattr(
        bii,
        "_read_current_build_markers",
        lambda *_: [
            (
                "build-000-fff/_SUCCESS",
                {
                    "dictionary_sha256": "dictionary",
                    "input_inventory_set_sha256": "wrong",
                },
            )
        ],
    )
    with pytest.raises(RuntimeError, match="input inventory set SHA"):
        bii._validate_build_marker_dictionary_sha(
            {"bucket": "b"}, "v", "dictionary", "expected"
        )


def test_dictionary_manifest_is_not_recreated_after_v3_outputs(monkeypatch):
    monkeypatch.setattr(bii, "_read_optional_r2_json", lambda *_: None)
    monkeypatch.setattr(bii, "_has_v3_id_build_state", lambda *_: True)
    upload = mock.Mock()
    monkeypatch.setattr(bii, "_upload_to_r2", upload)

    with pytest.raises(RuntimeError, match="Refusing to create"):
        bii.phase_build_locator_dictionary(
            {"bucket": "b"}, "v", "2026-06-17.0")
    upload.assert_not_called()


def test_dictionary_publication_orders_artifact_manifest_then_marker(monkeypatch):
    release = "2026-06-17.0"
    events = []

    class FakeConnection:
        def close(self):
            pass

    monkeypatch.setattr(bii, "_read_optional_r2_json", lambda *_: None)
    monkeypatch.setattr(bii, "_has_v3_id_build_state", lambda *_: False)
    monkeypatch.setattr(bii, "_r2_con", lambda *_: FakeConnection())
    source_files = [("places", "place", "part.parquet")]
    registry_inventory = bii._make_stage_inventory(
        "registry_range",
        release,
        {"prefix_start": "000", "prefix_end": "fff"},
        last_seen_releases=["2026-05-20.0"],
    )
    registry_reference, _ = bii._stage_inventory_reference(registry_inventory)
    release_inventory = bii._make_stage_inventory(
        "release_type",
        release,
        {"theme": "places", "feature_type": "place"},
        source_files=source_files,
    )
    release_reference, _ = bii._stage_inventory_reference(release_inventory)
    monkeypatch.setattr(
        bii,
        "_discover_current_release_source_files",
        lambda *_: source_files,
    )
    monkeypatch.setattr(
        bii,
        "_load_registry_inventory_fan_in",
        lambda *_args, **_kwargs: (
            ["2026-05-20.0"],
            [registry_reference],
            set(range(4096)),
        ),
    )
    monkeypatch.setattr(
        bii,
        "_load_release_inventory_fan_in",
        lambda *_: [release_reference],
    )

    def upload(_path, key):
        events.append(("upload", key))
        return None

    monkeypatch.setattr(bii, "_upload_to_r2", upload)
    monkeypatch.setattr(
        bii, "_write_staging_marker",
        lambda *_args, **_kwargs: events.append(("marker", "id-dictionaries")),
    )

    bii.phase_build_locator_dictionary({"bucket": "b"}, "v", release)
    assert events[0][0] == "upload"
    assert "/id-inventories/current_release_files-" in events[0][1]
    assert "/id-inventories/inventory-set-" in events[1][1]
    assert "/id-locator-dictionary-" in events[2][1]
    assert events[3] == ("upload", "b/v/id-locator-manifest.json")
    assert events[4] == ("marker", "id-dictionaries")


def test_locator_dictionary_rejects_reordering_and_bad_path():
    payload = bii._make_locator_dictionary(
        [("places", "place", "a.parquet"),
         ("places", "place", "b.parquet")],
        ["2026-05-20.0"], "2026-06-17.0")
    payload["source_files"].reverse()
    with pytest.raises(RuntimeError, match="Invalid locator dictionary"):
        bii._validate_locator_dictionary(payload, "2026-06-17.0")
    with pytest.raises(RuntimeError, match="Invalid source filename"):
        bii._make_locator_dictionary(
            [("places", "place", "nested/a.parquet")], [], "2026-06-17.0")


def test_compact_locator_mapping_is_one_based_and_fails_on_unseen_values():
    payload = bii._make_locator_dictionary(
        [("addresses", "address", "part.parquet")],
        ["2026-05-20.0"], "2026-06-17.0")
    source_path, release_path = bii._write_local_dictionary_tables(payload)
    con = duckdb.connect()
    try:
        union_query = """
            SELECT '00000000-0000-0000-0000-000000000001'::UUID AS id,
                   1::FLOAT bbox_xmin, 2::FLOAT bbox_ymin,
                   3::FLOAT bbox_xmax, 4::FLOAT bbox_ymax,
                   'address'::VARCHAR feature_type,
                   'part.parquet'::VARCHAR filename,
                   '2026-06-17.0'::VARCHAR last_seen_release,
                   false::BOOLEAN registry_member,
                   'addresses'::VARCHAR source_theme
            UNION ALL
            SELECT '00000000-0000-0000-0000-000000000002'::UUID,
                   1::FLOAT, 2::FLOAT, 3::FLOAT, 4::FLOAT,
                   NULL::VARCHAR, NULL::VARCHAR, '2026-05-20.0', true,
                   NULL::VARCHAR
        """
        mapped = bii._compact_locator_query(
            union_query, source_path, release_path)
        bii._assert_compact_locator_mapping(con, mapped, "000")
        assert con.execute(f"""
            SELECT source_file_id, last_seen_release_id
            FROM ({mapped}) ORDER BY id
        """).fetchall() == [(1, None), (None, 1)]

        unseen = union_query.replace("part.parquet", "new-part.parquet")
        with pytest.raises(RuntimeError, match="immutable locator dictionary"):
            bii._assert_compact_locator_mapping(
                con,
                bii._compact_locator_query(unseen, source_path, release_path),
                "000",
            )
    finally:
        con.close()
        Path(source_path).unlink(missing_ok=True)
        Path(release_path).unlink(missing_ok=True)


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
    reference = {
        "href": "./id-locator-dictionary-abc.json",
        "sha256": "abc",
    }
    metadata = bii._format_metadata(3, "2026-06-17.0", reference)
    assert metadata["format_version"] == 3
    assert metadata["overture_release"] == "2026-06-17.0"
    assert metadata["locator_dictionary"] == reference


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


def test_phase_metadata_preserves_exact_sizes_from_metadata_discovery(monkeypatch):
    uploaded = {}

    def capture(path, key):
        uploaded[key] = __import__("json").loads(path.read_text())
        return None

    monkeypatch.setattr(bii, "_detect_output_shard_format", lambda *_: 1)
    monkeypatch.setattr(bii, "_sum_build_marker_records", lambda *_: 7)
    monkeypatch.setattr(bii, "_upload_to_r2", capture)
    bii.phase_metadata(
        [("000", None, 1234, None)], 3, "v", "2026-06-17.0",
        {"bucket": "test"},
    )

    collection = uploaded["test/v/id-collection.json"]
    assert collection["items"]["000"]["size_bytes"] == 1234
    assert collection["summaries"]["total_size_bytes"] == 1234
    assert collection["summaries"]["total_records"] == 7


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
        (
            "_build_registry_stage_inventory",
            {
                "href": "./id-inventories/registry.json",
                "sha256": "b" * 64,
                "size_bytes": 100,
                "kind": "registry_range",
                "scope": {"prefix_start": "000", "prefix_end": "3ff"},
            },
        ),
        ("phase_partition_release_r2", None),
        ("phase_build_locator_dictionary", None),
        (
            "_load_locator_manifest_and_dictionary",
            (
                {},
                {
                    "input_inventory_set_sha256": "d" * 64,
                    "input_inventory_set": {
                        "href": "./id-inventories/inventory-set-" + "e" * 64 + ".json",
                        "sha256": "e" * 64,
                        "size_bytes": 100,
                        "inventories_count": 1,
                        "inventory_references_sha256": "d" * 64,
                    },
                },
                {"href": "./id-locator-dictionary-abc.json", "sha256": "abc"},
            ),
        ),
        (
            "_load_locator_dictionary_reference",
            {
                "href": "./id-locator-dictionary-abc.json",
                "sha256": "abc",
            },
        ),
        (
            "_load_locator_dictionary_binding",
            (
                {"href": "./id-locator-dictionary-abc.json", "sha256": "abc"},
                "d" * 64,
            ),
        ),
        ("_validate_build_marker_dictionary_sha", None),
        ("phase_build_r2", [("001", 10, 360, None, "a" * 64)]),
        ("phase_metadata", ({"001": {"record_count": 10}}, 10, [], 3)),
        ("_gather_shard_info_from_r2", [("001", 10, 360, None, "a" * 64)]),
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
    assert [call.args[3] for call in
            mocks["_write_staging_marker"].call_args_list] == [1024, 1024]


def test_range_stage_skips_when_range_marker_exists(pipeline):
    run, mocks = pipeline
    mocks["_read_staging_marker"].return_value = {
        "status": "complete", "format_version": 3}
    run(make_args(phase="stage-registry", prefix_start="000", prefix_end="3ff"))

    assert "id-partitioned-000-3ff" in read_marker_keys(mocks)
    mocks["phase_partition_r2"].assert_not_called()


def test_range_stage_writes_range_marker(pipeline):
    run, mocks = pipeline
    run(make_args(phase="stage-registry", prefix_start="000", prefix_end="3ff"))

    mocks["phase_partition_r2"].assert_called_once()
    assert written_marker_keys(mocks) == ["id-partitioned-000-3ff"]


def test_legacy_marker_never_skips_v3_stage_or_build(pipeline):
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
    assert extra == {
        "records": 10,
        "dictionary_sha256": "abc",
        "input_inventory_set_sha256": "d" * 64,
        "shards": {
            "001": {
                "record_count": 10,
                "size_bytes": 360,
                "sha256": "a" * 64,
            }
        },
    }


def test_matching_build_marker_sha_skips_build(pipeline):
    run, mocks = pipeline
    mocks["_read_staging_marker"].return_value = {
        "status": "complete",
        "format_version": 3,
        "dictionary_sha256": "abc",
        "input_inventory_set_sha256": "d" * 64,
    }
    run(make_args(phase="build", prefix_start="000", prefix_end="3ff"))
    mocks["phase_build_r2"].assert_not_called()


def test_mismatched_build_marker_sha_never_skips(pipeline):
    run, mocks = pipeline
    mocks["_read_staging_marker"].return_value = {
        "status": "complete",
        "format_version": 3,
        "dictionary_sha256": "wrong",
    }
    with pytest.raises(RuntimeError, match="does not match locator manifest SHA"):
        run(make_args(phase="build", prefix_start="000", prefix_end="3ff"))
    mocks["phase_build_r2"].assert_not_called()


def test_metadata_build_marker_mismatch_precedes_upload(monkeypatch):
    upload = mock.Mock()
    monkeypatch.setattr(bii, "_detect_output_shard_format", lambda *_: 3)
    monkeypatch.setattr(
        bii,
        "_load_locator_dictionary_binding",
        lambda *_: ({"sha256": "expected"}, "d" * 64),
    )
    monkeypatch.setattr(
        bii,
        "_validate_build_marker_dictionary_sha",
        mock.Mock(side_effect=RuntimeError("build marker SHA mismatch")),
    )
    monkeypatch.setattr(bii, "_upload_to_r2", upload)
    with pytest.raises(RuntimeError, match="build marker SHA mismatch"):
        bii.phase_metadata(
            [("000", 1, 32, None)],
            3,
            "v",
            "2026-06-17.0",
            {"bucket": "test"},
        )
    upload.assert_not_called()


def test_record_total_recovery_rejects_dictionary_sha_mismatch(monkeypatch):
    monkeypatch.setattr(
        bii, "_read_current_build_markers",
        lambda *_: [("build-000-3ff/_SUCCESS", {
            "records": 10,
            "dictionary_sha256": "wrong",
        })],
    )
    with pytest.raises(RuntimeError, match="does not match locator manifest SHA"):
        bii._sum_build_marker_records({"bucket": "b"}, "v", "expected")


def test_record_total_recovery_rejects_inventory_sha_mismatch(monkeypatch):
    monkeypatch.setattr(
        bii,
        "_read_current_build_markers",
        lambda *_: [
            (
                "build-000-3ff/_SUCCESS",
                {
                    "records": 10,
                    "dictionary_sha256": "dictionary",
                    "input_inventory_set_sha256": "wrong",
                },
            )
        ],
    )
    with pytest.raises(RuntimeError, match="input inventory set SHA"):
        bii._sum_build_marker_records({"bucket": "b"}, "v", "dictionary", "expected")


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
        "status": "complete",
        "format_version": 3,
        "dictionary_sha256": "abc",
        "input_inventory_set_sha256": "d" * 64,
    }
    run(make_args(phase=None))  # "all"

    mocks["phase_partition_r2"].assert_not_called()
    mocks["phase_partition_release_r2"].assert_not_called()
    # The phase validates the immutable artifact behind the current marker.
    mocks["phase_build_locator_dictionary"].assert_called_once()
    mocks["phase_build_r2"].assert_not_called()
    mocks["phase_metadata"].assert_not_called()


def test_full_run_executes_all_phases_and_markers(pipeline):
    run, mocks = pipeline
    run(make_args(phase=None))  # "all", no markers exist

    mocks["phase_partition_r2"].assert_called_once()
    mocks["phase_partition_release_r2"].assert_called_once()
    mocks["phase_build_locator_dictionary"].assert_called_once()
    mocks["phase_build_r2"].assert_called_once()
    mocks["phase_metadata"].assert_called_once()
    # ("id-release" is written inside phase_partition_release_r2, mocked here)
    assert written_marker_keys(mocks) == ["id-partitioned", "build", "metadata"]
    metadata_call = mocks["_write_staging_marker"].call_args_list[-1]
    assert metadata_call.kwargs["extra"] == {
        "records": 10,
        "dictionary_sha256": "abc",
        "input_inventory_set_sha256": "d" * 64,
    }
