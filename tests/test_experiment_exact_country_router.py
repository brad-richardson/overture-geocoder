"""Synthetic correctness tests for the research-only exact-country artifact."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import duckdb
import pytest

pytest.importorskip("shapely")
from shapely.geometry import MultiPolygon, Point, Polygon, box

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import experiment_exact_country_router as router


def _row(
    area_id: str,
    country: str,
    geometry,
    *,
    land: bool = True,
    territorial: bool = False,
    perspectives=None,
    division_id: str | None = None,
):
    return {
        "area_id": area_id,
        "division_id": division_id or "division-" + area_id,
        "area_version": 1,
        "division_version": 2,
        "country": country,
        "subtype": "country",
        "is_land": land,
        "is_territorial": territorial,
        "geometry": bytes(geometry.wkb),
        "perspectives_json": (
            None if perspectives is None else json.dumps(perspectives)
        ),
        "area_sources_json": json.dumps([{"dataset": "synthetic-area"}]),
        "geometry_sources_json": json.dumps([{"dataset": "synthetic-geometry"}]),
        "division_sources_json": json.dumps([{"dataset": "synthetic-division"}]),
        "division_country": country,
        "overture_release": "2026-06-17.0",
    }


def _write_area_parquet(path: Path, rows: list[dict]) -> None:
    connection = duckdb.connect()
    connection.execute(
        """
        CREATE TABLE areas(
            area_id VARCHAR, division_id VARCHAR, area_version INTEGER,
            division_version INTEGER, country VARCHAR, subtype VARCHAR,
            is_land BOOLEAN, is_territorial BOOLEAN, geometry BLOB,
            perspectives_json VARCHAR, area_sources_json VARCHAR,
            geometry_sources_json VARCHAR, division_sources_json VARCHAR,
            division_country VARCHAR, overture_release VARCHAR
        )
        """
    )
    connection.executemany(
        "INSERT INTO areas VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [tuple(row.values()) for row in rows],
    )
    connection.execute("COPY areas TO ? (FORMAT PARQUET)", [str(path)])
    connection.close()


def _build(tmp_path: Path, rows: list[dict]):
    source = tmp_path / "areas.parquet"
    artifact = tmp_path / "country-router.db"
    manifest = artifact.with_name(artifact.name + ".manifest.json")
    _write_area_parquet(source, rows)
    result = router.build_artifact(
        source,
        artifact,
        "2026-06-17.0",
    )
    return artifact, manifest, result


def test_same_country_land_territorial_and_dual_claims_dedupe(tmp_path):
    shape = box(0, 0, 10, 10)
    artifact, _, manifest = _build(
        tmp_path,
        [
            _row("us-land", "US", shape),
            _row("us-sea", "US", shape, land=False, territorial=True),
            _row("us-dual", "US", shape, land=True, territorial=True),
        ],
    )

    result = router.resolve_point(artifact, 5, 5)

    assert result["decision"] == "route"
    assert result["country"] == "US"
    assert result["claim_kinds"] == ["dual", "land", "territorial"]
    assert result["candidate_count"] == result["exact_tests"] == 3
    assert manifest["audit"]["dual_land_territorial_claim_rows"] == 1
    assert manifest["audit"]["unique_wkb_records"] == 1
    assert manifest["audit"]["deduplicated_wkb_references"] == 2


def test_cross_country_overlap_and_synthetic_claim_are_blockers(tmp_path):
    artifact, _, _ = _build(
        tmp_path,
        [
            _row("us", "US", box(0, 0, 10, 10)),
            _row("ca", "CA", box(5, 0, 15, 10)),
            _row("xz", "XZ", box(20, 0, 30, 10)),
        ],
    )

    overlap = router.resolve_point(artifact, 7, 5)
    synthetic = router.resolve_point(artifact, 25, 5)

    assert overlap["decision"] == "HEAD"
    assert overlap["reason"] == "multiple_countries"
    assert overlap["matched_countries"] == ["CA", "US"]
    assert synthetic["decision"] == "HEAD"
    assert synthetic["reason"] == "synthetic_country"


def test_country_codes_are_exactly_two_uppercase_letters(tmp_path):
    source = tmp_path / "areas.parquet"
    _write_area_parquet(source, [_row("bad", "X1", box(0, 0, 1, 1))])
    with pytest.raises(RuntimeError, match="invalid country code"):
        router.build_artifact(source, tmp_path / "bad.db", "2026-06-17.0")


def test_hole_island_enclave_and_exact_boundaries(tmp_path):
    country_with_hole = Polygon(
        [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)],
        [[(4, 4), (6, 4), (6, 6), (4, 6), (4, 4)]],
    )
    islands = MultiPolygon([box(20, 0, 21, 1), box(23, 0, 24, 1)])
    artifact, _, _ = _build(
        tmp_path,
        [
            _row("aa", "AA", country_with_hole),
            _row("bb", "BB", box(4.2, 4.2, 5.8, 5.8)),
            _row("cc", "CC", islands),
        ],
    )

    assert router.resolve_point(artifact, 2, 2)["country"] == "AA"
    assert router.resolve_point(artifact, 5, 5)["country"] == "BB"
    assert router.resolve_point(artifact, 22, 0.5)["reason"] == "no_match"
    assert router.resolve_point(artifact, 23.5, 0.5)["country"] == "CC"
    assert router.resolve_point(artifact, 4, 5)["reason"] == "boundary"
    assert router.resolve_point(artifact, 0, 0)["reason"] == "boundary"


def test_antimeridian_is_split_without_greenwich_false_match(tmp_path):
    dateline = Polygon(
        [
            (179, -2),
            (-179, -2),
            (-179, 2),
            (179, 2),
            (179, -2),
        ]
    )
    artifact, _, manifest = _build(tmp_path, [_row("dl", "DL", dateline)])

    assert router.resolve_point(artifact, 179.5, 0)["country"] == "DL"
    assert router.resolve_point(artifact, -179.5, 0)["country"] == "DL"
    assert router.resolve_point(artifact, 0, 0)["reason"] == "no_match"
    assert router.resolve_point(artifact, -180, 0)["country"] == "DL"
    assert router.resolve_point(artifact, 180, 0)["country"] == "DL"
    assert manifest["audit"]["normalized_components"] == 2
    assert manifest["audit"]["antimeridian_split_source_components"] == 1


@pytest.mark.parametrize(
    ("land", "territorial"),
    [(False, False), (None, True)],
)
def test_invalid_flags_abort_without_outputs(tmp_path, land, territorial):
    row = _row("bad", "US", box(0, 0, 1, 1))
    row["is_land"] = land
    row["is_territorial"] = territorial
    source = tmp_path / "areas.parquet"
    output = tmp_path / "router.db"
    manifest = tmp_path / "router.json"
    _write_area_parquet(source, [row])

    with pytest.raises(RuntimeError, match="is_land|at least one"):
        router.build_artifact(
            source,
            output,
            "2026-06-17.0",
            manifest_path=manifest,
        )

    assert not output.exists()
    assert not manifest.exists()


def test_corrupt_and_non_polygon_wkb_abort_build(tmp_path):
    corrupt = _row("bad", "US", box(0, 0, 1, 1))
    corrupt["geometry"] = b"not-wkb"
    source = tmp_path / "areas.parquet"
    _write_area_parquet(source, [corrupt])

    with pytest.raises(RuntimeError, match="not valid WKB"):
        router.build_artifact(source, tmp_path / "bad.db", "2026-06-17.0")

    source.unlink()
    _write_area_parquet(source, [_row("point", "US", Point(0, 0))])
    with pytest.raises(RuntimeError, match="not polygonal"):
        router.build_artifact(source, tmp_path / "point.db", "2026-06-17.0")


def test_corrupt_stored_wkb_and_bad_input_fall_back_to_head(tmp_path):
    artifact, _, _ = _build(tmp_path, [_row("us", "US", box(0, 0, 10, 10))])
    connection = sqlite3.connect(artifact)
    connection.execute("UPDATE geometries SET wkb = x'0102'")
    connection.commit()
    connection.close()

    assert router.resolve_point(artifact, 5, 5)["reason"] == "artifact_error"
    assert router.resolve_point(artifact, float("nan"), 5)["reason"] == "input_error"
    assert router.resolve_point(artifact, 181, 5)["reason"] == "input_error"
    assert (
        router.resolve_point(tmp_path / "missing.db", 5, 5)["reason"]
        == "artifact_error"
    )


@pytest.mark.parametrize(
    "mutation",
    ("country", "rtree", "valid_wkb"),
)
def test_manifest_hash_rejects_valid_artifact_tampering(tmp_path, mutation):
    artifact, _, _ = _build(tmp_path, [_row("us", "US", box(0, 0, 10, 10))])
    connection = sqlite3.connect(artifact)
    if mutation == "country":
        connection.execute("UPDATE claims SET country = 'CA'")
    elif mutation == "rtree":
        connection.execute("DELETE FROM component_rtree")
    else:
        connection.execute(
            "UPDATE geometries SET wkb = ?",
            [sqlite3.Binary(bytes(box(0, 0, 20, 20).wkb))],
        )
    connection.commit()
    connection.close()

    result = router.resolve_point(artifact, 5, 5)
    assert result["reason"] == "artifact_error"
    assert "does not match manifest" in result["errors"][0]


def test_missing_manifest_and_cross_artifact_cache_fail_closed(tmp_path):
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    first, first_manifest, _ = _build(first_dir, [_row("us", "US", box(0, 0, 1, 1))])
    second, _, _ = _build(second_dir, [_row("ca", "CA", box(10, 0, 11, 1))])
    shared_cache = {}
    assert (
        router.resolve_point(first, 0.5, 0.5, geometry_cache=shared_cache)["country"]
        == "US"
    )
    assert (
        router.resolve_point(second, 10.5, 0.5, geometry_cache=shared_cache)["country"]
        == "CA"
    )
    assert len(shared_cache) == 2

    first_manifest.unlink()
    assert router.resolve_point(first, 0.5, 0.5)["reason"] == "artifact_error"


def test_parent_perspectives_are_joined_and_block_routing(tmp_path):
    source = tmp_path / "areas.parquet"
    parent = tmp_path / "divisions.parquet"
    output = tmp_path / "router.db"
    _write_area_parquet(
        source,
        [_row("claim", "US", box(0, 0, 10, 10), division_id="parent")],
    )
    connection = duckdb.connect()
    connection.execute(
        "CREATE TABLE divisions(id VARCHAR, country VARCHAR, version INTEGER, "
        "subtype VARCHAR, perspectives_json VARCHAR)"
    )
    connection.execute(
        "INSERT INTO divisions VALUES ('parent', 'US', 7, 'country', ?)",
        [json.dumps({"mode": "accepted_by", "countries": ["US"]})],
    )
    connection.execute("COPY divisions TO ? (FORMAT PARQUET)", [str(parent)])
    connection.close()

    manifest = router.build_artifact(
        source,
        output,
        "2026-06-17.0",
        division_path=parent,
    )

    result = router.resolve_point(output, 5, 5)
    assert result["reason"] == "perspective_claim"
    assert manifest["source"]["division"]["country_rows"] == 1
    assert manifest["audit"]["division_perspective_claim_rows"] == 1


def test_missing_columns_and_parent_mismatch_fail_closed(tmp_path):
    source = tmp_path / "missing.parquet"
    connection = duckdb.connect()
    connection.execute("CREATE TABLE incomplete(area_id VARCHAR, country VARCHAR)")
    connection.execute("INSERT INTO incomplete VALUES ('x', 'US')")
    connection.execute("COPY incomplete TO ? (FORMAT PARQUET)", [str(source)])
    connection.close()
    with pytest.raises(RuntimeError, match="missing required 'division_id'"):
        router.build_artifact(source, tmp_path / "missing.db", "2026-06-17.0")

    joined_source = tmp_path / "joined-source.parquet"
    unjoined_source = tmp_path / "unjoined-source.parquet"
    _write_area_parquet(joined_source, [_row("unjoined", "US", box(0, 0, 1, 1))])
    connection = duckdb.connect()
    joined_sql = str(joined_source).replace("'", "''")
    unjoined_sql = str(unjoined_source).replace("'", "''")
    connection.execute(
        "COPY (SELECT * EXCLUDE (division_country) "
        f"FROM read_parquet('{joined_sql}')) "
        f"TO '{unjoined_sql}' (FORMAT PARQUET)"
    )
    connection.close()
    with pytest.raises(RuntimeError, match="must include division_country"):
        router.build_artifact(unjoined_source, tmp_path / "unjoined.db", "2026-06-17.0")

    joined = tmp_path / "joined-mismatch.parquet"
    joined_row = _row("joined", "US", box(0, 0, 1, 1))
    joined_row["division_country"] = "CA"
    _write_area_parquet(joined, [joined_row])
    with pytest.raises(RuntimeError, match="disagrees with joined parent country"):
        router.build_artifact(joined, tmp_path / "joined-mismatch.db", "2026-06-17.0")

    area = tmp_path / "area.parquet"
    _write_area_parquet(
        area, [_row("claim", "US", box(0, 0, 1, 1), division_id="parent")]
    )
    missing_parents = tmp_path / "missing-parents.parquet"
    connection = duckdb.connect()
    connection.execute(
        "CREATE TABLE divisions(id VARCHAR, country VARCHAR, subtype VARCHAR)"
    )
    connection.execute("INSERT INTO divisions VALUES ('other', 'US', 'country')")
    connection.execute("COPY divisions TO ? (FORMAT PARQUET)", [str(missing_parents)])
    connection.close()
    with pytest.raises(RuntimeError, match="parent division 'parent' is missing"):
        router.build_artifact(
            area,
            tmp_path / "missing-parent.db",
            "2026-06-17.0",
            division_path=missing_parents,
        )

    mismatched_parents = tmp_path / "mismatched-parents.parquet"
    connection = duckdb.connect()
    connection.execute(
        "CREATE TABLE divisions(id VARCHAR, country VARCHAR, subtype VARCHAR)"
    )
    connection.execute("INSERT INTO divisions VALUES ('parent', 'CA', 'country')")
    connection.execute(
        "COPY divisions TO ? (FORMAT PARQUET)", [str(mismatched_parents)]
    )
    connection.close()
    with pytest.raises(RuntimeError, match="disagrees with parent country"):
        router.build_artifact(
            area,
            tmp_path / "mismatched-parent.db",
            "2026-06-17.0",
            division_path=mismatched_parents,
        )


def test_claim_identity_is_stable_across_parquet_row_order(tmp_path):
    rows = [
        _row("z-area", "US", box(0, 0, 1, 1)),
        _row("a-area", "CA", box(2, 0, 3, 1)),
    ]
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    first, _, _ = _build(first_dir, rows)
    second, _, _ = _build(second_dir, list(reversed(rows)))

    def identities(path):
        connection = sqlite3.connect(path)
        try:
            return connection.execute(
                "SELECT claim_id, source_row, area_id FROM claims ORDER BY claim_id"
            ).fetchall()
        finally:
            connection.close()

    assert (
        identities(first)
        == identities(second)
        == [
            (1, 1, "a-area"),
            (2, 2, "z-area"),
        ]
    )


def test_release_is_bound_to_parquet_rows(tmp_path):
    source = tmp_path / "wrong-release.parquet"
    row = _row("us", "US", box(0, 0, 1, 1))
    row["overture_release"] = "2026-05-20.0"
    _write_area_parquet(source, [row])
    with pytest.raises(RuntimeError, match="do not match declared release"):
        router.build_artifact(source, tmp_path / "wrong.db", "2026-06-17.0")


def test_manifest_hash_and_benchmark_metrics_reconcile(tmp_path):
    artifact, manifest_path, manifest = _build(
        tmp_path, [_row("us", "US", box(0, 0, 10, 10))]
    )
    assert manifest["artifact"]["sha256"] == router.sha256_file(artifact)
    assert manifest["artifact"]["size_bytes"] == artifact.stat().st_size
    assert json.loads(manifest_path.read_text())["artifact"] == manifest["artifact"]
    audit_path = artifact.with_name(artifact.name + ".audit.json")
    audit = json.loads(audit_path.read_text())
    assert manifest["audit_sidecar"]["sha256"] == router.sha256_file(audit_path)
    assert manifest["audit_sidecar"]["size_bytes"] == audit_path.stat().st_size
    assert audit["source_claim_count"] == 1
    assert audit["retained_claim_count"] == 1
    assert set(audit["claims_by_area_id"]) == {"us"}
    assert audit["claims_by_area_id"]["us"]["area_sources_json"]
    queries = tmp_path / "queries.json"
    queries.write_text(
        json.dumps(
            [
                {"label": "inside", "lon": 5, "lat": 5, "expected": "US"},
                {
                    "label": "outside",
                    "lon": 20,
                    "lat": 20,
                    "expected_reason": "no_match",
                },
            ]
        )
    )

    report = router.benchmark_artifact(
        artifact, queries, iterations=2, open_iterations=2
    )

    assert report["queries"]["mismatches"] == []
    assert report["decision_reasons"] == {"route": 1, "no_match": 1}
    assert report["candidate_fanout"]["max"] == 1
    assert report["artifact"]["sha256"] == manifest["artifact"]["sha256"]
    assert report["resource_proxy"]["observed_high_water_delta_bytes"] >= 0
    assert "not incremental Worker" in report["resource_proxy"]["warning"]


def test_territorial_primary_policy_and_simplification_are_explicit(tmp_path):
    source = tmp_path / "areas.parquet"
    artifact = tmp_path / "territorial.db"
    shape = Polygon([(0, 0), (2, 0), (2, 0.1), (1, 0.11), (0, 0.1), (0, 0)])
    _write_area_parquet(
        source,
        [
            _row("us-land", "US", shape),
            _row("us-territory", "US", shape, land=False, territorial=True),
            _row("ca-dual", "CA", box(10, 0, 11, 1), territorial=True),
        ],
    )

    manifest = router.build_artifact(
        source,
        artifact,
        "2026-06-17.0",
        claim_policy="territorial-primary",
        simplify_tolerance=0.01,
    )

    assert manifest["claim_policy"] == "territorial-primary"
    assert manifest["simplify_tolerance_degrees"] == 0.01
    assert manifest["geometry_semantics"].endswith("not-an-exact-oracle")
    assert manifest["audit"]["source_claim_rows"] == 3
    assert manifest["audit"]["retained_claim_rows"] == 2
    connection = sqlite3.connect(artifact)
    assert connection.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 2
    assert {
        row[1] for row in connection.execute("PRAGMA table_info(claims)")
    }.isdisjoint(
        {"area_sources_json", "geometry_sources_json", "division_sources_json"}
    )
    assert (
        connection.execute("SELECT COUNT(*) FROM components").fetchone()[0]
        == manifest["audit"]["normalized_components"]
    )
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM components c LEFT JOIN claims q USING (claim_id) "
            "WHERE q.claim_id IS NULL"
        ).fetchone()[0]
        == 0
    )
    connection.close()
    audit_path = artifact.with_name(artifact.name + ".audit.json")
    audit = json.loads(audit_path.read_text())
    assert audit["source_claim_count"] == 3
    assert audit["retained_claim_count"] == 2
    assert sum(claim["retained"] for claim in audit["claims_by_area_id"].values()) == 2
    result = router.resolve_point(artifact, 1, 0.05)
    assert result["country"] == "US"
    assert result["claim_policy"] == "territorial-primary"
    assert result["geometry_semantics"] == "topology-preserving-simplified"


def test_territorial_primary_requires_one_claim_per_country(tmp_path):
    source = tmp_path / "areas.parquet"
    _write_area_parquet(
        source,
        [
            _row("us-land", "US", box(0, 0, 1, 1)),
            _row("us-territory-a", "US", box(0, 0, 2, 2), land=False, territorial=True),
            _row("us-territory-b", "US", box(0, 0, 3, 3), land=False, territorial=True),
        ],
    )
    with pytest.raises(RuntimeError, match="exactly one territorial claim"):
        router.build_artifact(
            source,
            tmp_path / "bad.db",
            "2026-06-17.0",
            claim_policy="territorial-primary",
        )


def test_overwrite_rolls_back_the_complete_output_set(tmp_path, monkeypatch):
    artifact, manifest_path, _ = _build(tmp_path, [_row("us", "US", box(0, 0, 1, 1))])
    audit_path = artifact.with_name(artifact.name + ".audit.json")
    original = {
        path: path.read_bytes() for path in (artifact, audit_path, manifest_path)
    }
    replacement_source = tmp_path / "replacement.parquet"
    _write_area_parquet(replacement_source, [_row("ca", "CA", box(10, 0, 11, 1))])
    real_replace = router.os.replace
    failed = False

    def fail_once(source, destination):
        nonlocal failed
        if (
            not failed
            and Path(destination) == audit_path
            and ".staged-" in Path(source).name
        ):
            failed = True
            raise OSError("injected audit publication failure")
        return real_replace(source, destination)

    monkeypatch.setattr(router.os, "replace", fail_once)
    with pytest.raises(OSError, match="injected audit publication failure"):
        router.build_artifact(
            replacement_source,
            artifact,
            "2026-06-17.0",
            overwrite=True,
        )

    assert failed
    for path, expected in original.items():
        assert path.read_bytes() == expected
    assert router.resolve_point(artifact, 0.5, 0.5)["country"] == "US"
    assert not list(tmp_path.glob(".*.staged-*"))
    assert not list(tmp_path.glob(".*.backup-*"))


def test_deterministic_compare_reports_false_unique_routes(tmp_path):
    oracle_dir = tmp_path / "oracle"
    candidate_dir = tmp_path / "candidate"
    oracle_dir.mkdir()
    candidate_dir.mkdir()
    oracle, _, _ = _build(
        oracle_dir, [_row("us", "US", box(0, 0, 1, 1), territorial=True)]
    )
    candidate, _, _ = _build(
        candidate_dir, [_row("us", "US", box(-1, -1, 2, 2), territorial=True)]
    )

    with pytest.raises(RuntimeError, match="do not share one Overture release"):
        router.compare_artifacts(
            oracle,
            [("expanded", candidate)],
            boundary_source=oracle,
            seed=123,
            global_points=0,
            boundary_points=1,
            jitters=(0.1,),
        )

    first = router.compare_artifacts(
        oracle,
        [("expanded", candidate)],
        boundary_source=oracle,
        seed=123,
        global_points=0,
        boundary_points=1,
        jitters=(0.1,),
        allow_cross_source=True,
    )
    second = router.compare_artifacts(
        oracle,
        [("expanded", candidate)],
        boundary_source=oracle,
        seed=123,
        global_points=0,
        boundary_points=1,
        jitters=(0.1,),
        allow_cross_source=True,
    )

    assert first["corpus"]["sha256"] == second["corpus"]["sha256"]
    assert first["corpus"]["counts"] == {
        "boundary-derived": 1,
        "boundary-jitter": 4,
    }
    assert first["corpus"]["boundary_source_reason_counts"] == {"boundary": 1}
    assert first["corpus"]["boundary_source_accepted_reason_counts"] == {"boundary": 1}
    comparison = first["comparisons"][0]
    assert first["cross_source_comparison"] is True
    assert comparison["totals"]["queries"] == 5
    assert comparison["totals"]["false_unique_routes"] >= 1
    assert comparison["groups"]["boundary-derived"]["false_unique_routes"] == 1


def test_compare_cli_argument_parsers_fail_closed():
    assert router._candidate_argument("exact=/tmp/exact.db") == (
        "exact",
        Path("/tmp/exact.db"),
    )
    assert router._jitter_argument("0.1,0.2") == (0.1, 0.2)
    with pytest.raises(Exception, match="LABEL=PATH"):
        router._candidate_argument("missing-label-separator")
    with pytest.raises(Exception, match="jitter"):
        router._jitter_argument("")


def test_expected_null_is_enforced_and_benchmark_cli_can_fail(tmp_path):
    artifact, _, _ = _build(tmp_path, [_row("us", "US", box(0, 0, 1, 1))])
    queries = tmp_path / "queries.json"
    queries.write_text(json.dumps([{"lon": 0.5, "lat": 0.5, "expected": None}]))
    report = router.benchmark_artifact(
        artifact, queries, iterations=1, open_iterations=1
    )
    assert report["queries"]["mismatches"] == [
        {"query": 1, "expected": None, "actual": "US"}
    ]

    report_path = tmp_path / "report.json"
    with pytest.raises(SystemExit) as exit_info:
        router.main(
            [
                "benchmark",
                "--artifact",
                str(artifact),
                "--queries",
                str(queries),
                "--iterations",
                "1",
                "--open-iterations",
                "1",
                "--fail-on-mismatch",
                "--report",
                str(report_path),
            ]
        )
    assert exit_info.value.code == 1
    assert report_path.is_file()
