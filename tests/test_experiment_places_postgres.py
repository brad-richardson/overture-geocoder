"""Tests for the PostgreSQL Places experiment generator/model."""

import importlib.util
import json
import sys
from pathlib import Path


SCRIPT = Path(__file__).parent.parent / "scripts" / "experiment_places_postgres.py"
spec = importlib.util.spec_from_file_location("experiment_places_postgres", SCRIPT)
experiment = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = experiment
spec.loader.exec_module(experiment)


def rows():
    source = [
        {
            "id": "a",
            "name": "Starbucks",
            "category": "coffee_shop",
            "city": "San Francisco",
            "region": "CA",
            "country": "US",
            "confidence": 0.9,
        },
        {
            "id": "b",
            "name": "Golden Gate Hotel",
            "category": "hotel",
            "city": "San Francisco",
            "region": "CA",
            "country": "US",
            "confidence": 0.8,
        },
    ]
    return [experiment.clean_row(row, number) for number, row in enumerate(source, 1)]


def test_schema_uses_immutable_release_partition_and_catalog_pointer():
    sql = experiment.schema_sql("fixture-1", "release-1")
    assert sql.startswith("-- Offline PlanetScale/PostgreSQL Places spike")
    assert "BEGIN;\nDROP SCHEMA" in sql
    assert sql.rstrip().endswith("COMMIT;")
    assert "PARTITION BY LIST (release_id)" in sql
    assert "search_document tsvector GENERATED ALWAYS" in sql
    assert "USING gin (search_document)" in sql
    assert "normalized_name text_pattern_ops" in sql
    assert "_category\n" in sql
    assert "_context\n" in sql
    assert "CREATE TABLE places_planetscale_spike.catalog" in sql


def test_queries_bind_release_for_partition_pruning_and_cover_shapes():
    queries = experiment.representative_queries("fixture-1")
    assert set(queries) == {
        "name_prefix",
        "token_exact",
        "token_prefix",
        "category",
        "context_token",
    }
    assert all("release_id = 'fixture-1'" in query for query in queries.values())
    assert "to_tsquery('simple', 'golden & gat:*')" in queries["token_prefix"]
    assert "plainto_tsquery('simple', 'warfield hotel')" in queries["token_exact"]
    assert "category = 'hotel'" in queries["category"]
    assert "locality = 'San Francisco'" in queries["context_token"]


def test_fixture_model_is_explicitly_not_postgres_storage():
    model = experiment.fixture_model(rows())
    assert model["row_count"] == 2
    assert model["model_candidate_counts"]["name_prefix_starb"] == 1
    assert model["model_candidate_counts"]["category_hotel"] == 1
    assert "not PostgreSQL storage" in model["linear_shape_only"]["warning"]


def test_non_postgres_psql_is_rejected(monkeypatch):
    monkeypatch.setattr(experiment.shutil, "which", lambda _: "/fake/psql")

    class Result:
        returncode = 1
        stdout = ""
        stderr = "node application error"

    monkeypatch.setattr(experiment.subprocess, "run", lambda *args, **kwargs: Result())
    assert experiment.usable_psql() == (False, "PATH psql is not PostgreSQL psql")


def test_cli_writes_model_only_artifacts(tmp_path, monkeypatch):
    source = tmp_path / "places.jsonl"
    source.write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                {"id": "a", "name": "Starbucks", "category": "coffee_shop"},
                {"id": "b", "name": "Hotel", "category": "hotel"},
            ]
        )
    )
    outputs = {
        name: tmp_path / name
        for name in ("schema.sql", "queries.sql", "report.json", "report.md")
    }
    monkeypatch.setattr(experiment, "usable_psql", lambda: (False, "psql not found"))
    result = experiment.main(
        [
            str(source),
            "--schema-out",
            str(outputs["schema.sql"]),
            "--queries-out",
            str(outputs["queries.sql"]),
            "--json-out",
            str(outputs["report.json"]),
            "--markdown-out",
            str(outputs["report.md"]),
        ]
    )
    assert result == 0
    report = json.loads(outputs["report.json"].read_text())
    assert report["database_execution"]["measured"] is False
    assert report["unmeasured_claims"]
    assert "model-only" in outputs["report.md"].read_text()


def test_database_execution_requires_explicit_destructive_confirmation(
    tmp_path, monkeypatch
):
    source = tmp_path / "places.jsonl"
    source.write_text(json.dumps({"id": "a", "name": "Cafe"}) + "\n")
    outputs = [tmp_path / name for name in ("schema.sql", "queries.sql", "out.json", "out.md")]
    monkeypatch.setattr(experiment, "usable_psql", lambda: (True, "PostgreSQL psql"))
    try:
        experiment.main(
            [
                str(source),
                "--database-url",
                "postgresql://scratch.invalid/db",
                "--schema-out",
                str(outputs[0]),
                "--queries-out",
                str(outputs[1]),
                "--json-out",
                str(outputs[2]),
                "--markdown-out",
                str(outputs[3]),
            ]
        )
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("database execution proceeded without confirmation")
