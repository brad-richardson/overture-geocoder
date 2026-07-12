"""Focused invariants for compact ID v3 scale gates."""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import benchmark_id_locator_scale as scale  # noqa: E402


def make_registry_prefix(path: Path, rows: int = 120_000) -> Path:
    connection = duckdb.connect()
    connection.execute(f"""
        COPY (
          SELECT md5(i::VARCHAR)::UUID AS id,
                 1::FLOAT bbox_xmin, 2::FLOAT bbox_ymin,
                 3::FLOAT bbox_xmax, 4::FLOAT bbox_ymax,
                 CASE WHEN i % 20 = 0 THEN NULL ELSE 'address' END::VARCHAR
                   AS feature_type,
                 CASE WHEN i % 20 = 0 THEN NULL
                      ELSE 'part-' || LPAD((i % 23)::VARCHAR, 5, '0')
                           || '.zstd.parquet' END::VARCHAR AS filename,
                 CASE WHEN i % 20 = 0 THEN '2026-05-20.0'
                      ELSE '2026-06-17.0' END::VARCHAR AS last_seen_release,
                 TRUE::BOOLEAN AS registry_member,
                 CASE WHEN i % 20 = 0 THEN NULL ELSE 'addresses' END::VARCHAR
                   AS source_theme,
                 '0a1'::VARCHAR AS prefix
          FROM range({rows}) AS values(i)
        ) TO '{path}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    connection.close()
    return path


def test_prefix_filter_is_bounded_and_handles_final_prefix():
    assert scale.prefix_filter("0a1") == "id >= '0a1' AND id < '0a2'"
    assert scale.prefix_filter("fff") == "id >= 'fff'"
    with pytest.raises(ValueError):
        scale.next_prefix("not-hex")


def test_footer_read_plan_models_initial_path_and_exact_retry():
    fits = scale.footer_read_plan(1_000_000, 7_321)
    assert fits["footer_range_reads"] == 1
    assert fits["total_footer_range_bytes"] == 32 * 1024

    overflow = scale.footer_read_plan(1_000_000, 40_000)
    assert overflow["footer_retry_required"] is True
    assert overflow["footer_retry_requested_bytes"] == 40_000
    assert overflow["footer_range_reads"] == 2
    assert overflow["total_footer_range_bytes"] == 32 * 1024 + 40_000

    small_file = scale.footer_read_plan(10_000, 9_000)
    assert small_file["initial_suffix_returned_bytes"] == 10_000
    assert small_file["footer_range_reads"] == 1


def test_real_shape_prefix_transcodes_without_dropping_rows(tmp_path: Path):
    source = make_registry_prefix(tmp_path / "registry.parquet")
    output = tmp_path / "compact.parquet"
    connection = duckdb.connect()
    dictionary = scale.dictionary_from_prefixes(connection, [source], "2026-06-17.0")
    metrics = scale.write_compact_prefix(
        connection, source, output, dictionary, row_group_size=50_000
    )
    report = scale.summarize_compact_prefix(connection, output)
    baseline = scale.write_v1_prefix(
        connection, source, tmp_path / "v1.parquet", row_group_size=50_000
    )
    connection.close()

    assert metrics["rows"] == 120_000
    assert dictionary["source_files_count"] == 23
    assert dictionary["last_seen_releases_count"] == 1
    assert report["row_groups"] == 3
    assert report["footer_range_reads"] == 1
    assert report["cold_lookup_range_bytes"]["p50"] > 32 * 1024
    assert metrics["bytes"] > baseline["v1_bytes"]


def test_real_shape_prefix_allows_empty_historical_dictionary(tmp_path: Path):
    source = tmp_path / "current-only.parquet"
    output = tmp_path / "compact.parquet"
    connection = duckdb.connect()
    connection.execute(f"""
        COPY (
          SELECT md5(i::VARCHAR)::UUID id,
                 1::FLOAT bbox_xmin, 2::FLOAT bbox_ymin,
                 3::FLOAT bbox_xmax, 4::FLOAT bbox_ymax,
                 'address'::VARCHAR feature_type,
                 'part-00000.zstd.parquet'::VARCHAR filename,
                 '2026-06-17.0'::VARCHAR last_seen_release,
                 TRUE::BOOLEAN registry_member,
                 'addresses'::VARCHAR source_theme,
                 '0a1'::VARCHAR prefix
          FROM range(100) values(i)
        ) TO '{source}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    dictionary = scale.dictionary_from_prefixes(connection, [source], "2026-06-17.0")
    metrics = scale.write_compact_prefix(
        connection, source, output, dictionary, row_group_size=50_000
    )
    connection.close()

    assert dictionary["last_seen_releases_count"] == 0
    assert metrics["rows"] == 100


def test_dictionary_cold_and_cached_proxy_is_bounded(tmp_path: Path):
    connection = duckdb.connect()
    dictionary = scale.dictionary_from_prefixes(
        connection,
        [make_registry_prefix(tmp_path / "registry.parquet", 1000)],
        "2026-06-17.0",
    )
    connection.close()
    report = scale.benchmark_dictionary_cold_cached(dictionary, 5)

    assert report["dictionary_bytes"] < 1024 * 1024
    assert report["cold_mock_fetch_sha_parse_validate_us"]["p50"] > 0
    assert report["cached_dictionary_lookup_us"]["p50"] > 0
    assert "excludes network" in report["scope_warning"]


def test_inventory_recommendation_rejects_footer_as_completeness_proof():
    report = scale.inventory_recommendation([{"rows": 10}, {"rows": 20}])
    assert report["measured_real_prefix_rows"] == 30
    assert "cannot prove" in report["limitation"]
    assert any("missing" in value for value in report["requirements"])


def test_markdown_preserves_completed_gates_when_historical_is_blocked():
    report = {
        "safety": "local only",
        "dictionary_discovery": {
            "release": {"distinct_source_tuples": 972, "elapsed_seconds": 1.0},
            "historical": {"status": "blocked", "error": "deadline"},
            "dictionary": {"status": "timing_proxy_only", "bytes": 90_000},
        },
        "prefixes": [],
        "dictionary_cold_cached": {
            "dictionary_bytes": 90_000,
            "cold_mock_fetch_sha_parse_validate_us": {"p50": 1.0, "p95": 2.0},
            "cached_dictionary_lookup_us": {"p50": 0.1, "p95": 0.2},
            "scope_warning": "proxy",
        },
        "inventory_recommendation": {"design": "fan-in", "limitation": "audit"},
    }
    markdown = scale.render_markdown(report)
    assert "Release tuples: **972**" in markdown
    assert "Historical releases: **blocked**" in markdown
    assert "timing_proxy_only" in markdown
