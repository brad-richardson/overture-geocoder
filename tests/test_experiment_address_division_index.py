from __future__ import annotations

import importlib.util
import struct
import sys
from pathlib import Path

import duckdb
import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "experiment_address_division_index.py"
SPEC = importlib.util.spec_from_file_location(
    "experiment_address_division_index", SCRIPT
)
assert SPEC and SPEC.loader
experiment = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(experiment)


def test_uvarint_boundaries():
    assert experiment.encode_uvarint(0) == b"\x00"
    assert experiment.encode_uvarint(127) == b"\x7f"
    assert experiment.encode_uvarint(128) == b"\x80\x01"
    assert experiment.encode_uvarint(16_384) == b"\x80\x80\x01"


def test_text_encoding_preserves_unicode():
    encoded = experiment.encode_text("Montréal")
    assert encoded[0] == len("Montréal".encode())
    assert encoded[1:].decode() == "Montréal"


@pytest.mark.parametrize("value", [" İstanbul ", "ΟΣ", "Cafe\u0301", "  MAIN\tSt  "])
def test_python_and_duckdb_normalization_contract_match(value):
    connection = duckdb.connect()
    actual = connection.execute(
        f"SELECT {experiment.normalize_expression('?')}", [value]
    ).fetchone()[0]
    connection.close()
    assert actual == experiment.normalize(value)


def test_chain_encoding_uses_compact_uuid_bytes():
    signature = (
        "region:00000000-0000-0000-0000-000000000001|"
        "locality:00000000-0000-0000-0000-000000000002"
    )
    encoded = experiment.encode_chain(signature)
    assert len(encoded) == 1 + 2 * 17
    assert encoded[0] == 2
    assert encoded[1] == experiment.SUBTYPE_CODES["region"]
    assert encoded[18] == experiment.SUBTYPE_CODES["locality"]


def test_invalid_feature_id_fails_closed():
    with pytest.raises(ValueError, match="invalid UUID"):
        experiment.encode_feature_id("not-a-uuid")


def test_coordinate_encoding_fits_signed_i32():
    lon = experiment.encode_coordinate(-71.0589, longitude=True)
    lat = experiment.encode_coordinate(42.3601, longitude=False)
    assert struct.pack("<ii", lon, lat)


def test_indexed_blob_writes_terminal_offset(tmp_path):
    index_path = tmp_path / "index.bin"
    blob_path = tmp_path / "blob.bin"
    with index_path.open("wb") as index_file, blob_path.open("wb") as blob_file:
        count = experiment.write_indexed_blob([b"abc", b"defgh"], index_file, blob_file)
    assert count == 2
    assert struct.unpack("<QQQ", index_path.read_bytes()) == (0, 3, 8)
    assert blob_path.read_bytes() == b"abcdefgh"


def build_fixture_artifact(tmp_path: Path, name: str = "fixture.aidx") -> Path:
    connection = duckdb.connect()
    connection.execute(
        "CREATE TABLE chain_dim(chain_id INTEGER, chain_signature VARCHAR)"
    )
    connection.execute(
        "INSERT INTO chain_dim VALUES (0, ''), (1, ?)",
        [
            "region:00000000-0000-0000-0000-000000000001|"
            "locality:00000000-0000-0000-0000-000000000002|"
            "locality:00000000-0000-0000-0000-000000000003"
        ],
    )
    connection.execute("""
        CREATE TABLE context_dim(
            context_id INTEGER, state_norm VARCHAR, city_norm VARCHAR,
            postal_city_norm VARCHAR, postcode_norm VARCHAR,
            dominant_chain_id INTEGER
        )
    """)
    connection.execute(
        "INSERT INTO context_dim VALUES (0, 'ma', 'boston', 'boston', '02108', 1)"
    )
    connection.execute("""
        CREATE TABLE street_dim(
            group_id INTEGER, context_id INTEGER, street_norm VARCHAR,
            display_street VARCHAR, record_count INTEGER
        )
    """)
    connection.execute("INSERT INTO street_dim VALUES (0, 0, 'main st', 'Main St', 3)")
    connection.execute("""
        CREATE TABLE label_dim(
            label_id INTEGER, state VARCHAR, city VARCHAR, postal_city VARCHAR,
            postcode VARCHAR, street VARCHAR
        )
    """)
    connection.execute("""
        INSERT INTO label_dim VALUES
            (0, 'MA', 'Boston', 'Boston', '02108', 'Main St'),
            (1, 'Massachusetts', 'BOSTON', 'Boston', '02108', 'MAIN ST')
    """)
    connection.execute("""
        CREATE TABLE indexed_addresses(
            context_id INTEGER, street_norm VARCHAR, feature_id VARCHAR,
            lon DOUBLE, lat DOUBLE, number VARCHAR, unit VARCHAR,
            number_norm VARCHAR, unit_norm VARCHAR, chain_id INTEGER,
            dominant_chain_id INTEGER, label_id INTEGER
        )
    """)
    rows = [
        ("00000000-0000-0000-0000-000000000010", "1", "1A", 0),
        ("00000000-0000-0000-0000-000000000011", "1", "1A", 1),
        ("00000000-0000-0000-0000-000000000012", "1", "2B", 0),
    ]
    for offset, (feature_id, number, unit, label_id) in enumerate(rows):
        connection.execute(
            "INSERT INTO indexed_addresses VALUES (0, 'main st', ?, ?, 42.0, ?, ?, ?, ?, 1, 1, ?)",
            [
                feature_id,
                -71.0 + offset / 1000,
                number,
                unit,
                number,
                unit.lower(),
                label_id,
            ],
        )
    artifact = tmp_path / name
    experiment.build_artifact(connection, artifact)
    connection.close()
    return artifact


def test_artifact_exact_unit_duplicate_and_no_result_lookup(tmp_path):
    artifact = build_fixture_artifact(tmp_path)
    with experiment.AddressArtifact(artifact) as reader:
        common = {
            "state": "MA",
            "city": "Boston",
            "postal_city": "Boston",
            "postcode": "02108",
            "street": "MAIN ST",
            "number": "1",
        }
        duplicates = reader.lookup(**common, unit="1a")
        all_units = reader.lookup(**common)
        missing = reader.lookup(**common, unit="missing")

    assert [row["feature_id"] for row in duplicates] == [
        "00000000-0000-0000-0000-000000000010",
        "00000000-0000-0000-0000-000000000011",
    ]
    assert len(all_units) == 3
    assert missing == []
    assert duplicates[1]["source_labels"]["state"] == "Massachusetts"
    locality_ids = [
        item["division_id"]
        for item in duplicates[0]["division_chain"]
        if item["subtype"] == "locality"
    ]
    assert locality_ids == [
        "00000000-0000-0000-0000-000000000002",
        "00000000-0000-0000-0000-000000000003",
    ]


def test_artifact_rejects_truncation(tmp_path):
    artifact = build_fixture_artifact(tmp_path)
    artifact.write_bytes(artifact.read_bytes()[:-1])
    with pytest.raises(ValueError, match="size does not match"):
        experiment.AddressArtifact(artifact)


def test_artifact_build_is_deterministic(tmp_path):
    first = build_fixture_artifact(tmp_path, "first.aidx")
    second = build_fixture_artifact(tmp_path, "second.aidx")
    assert first.read_bytes() == second.read_bytes()


def test_prepare_database_preserves_overlap_and_classifies_boundary(tmp_path):
    addresses = tmp_path / "addresses.parquet"
    divisions = tmp_path / "divisions.parquet"
    connection = duckdb.connect()
    connection.execute("INSTALL spatial; LOAD spatial")
    connection.execute("""
        CREATE TABLE source_addresses AS
        SELECT * FROM (VALUES
          ('00000000-0000-0000-0000-000000000010', 0.0, 0.5, 'Main St', '1', '', '02108', 'MA', 'Box', 'Box'),
          ('00000000-0000-0000-0000-000000000011', 0.5, 0.5, 'Main St', '2', '', '02108', 'MA', 'Box', 'Box')
        ) AS t(id, lon, lat, street, number, unit, postcode, state, city, postal_city)
    """)
    connection.execute("""
        CREATE TABLE source_divisions AS
        SELECT * FROM (VALUES
          ('00000000-0000-0000-0000-000000000001', 'locality', 'Box', 1.0,
           ST_GeomFromText('POLYGON ((0 0, 1 0, 1 1, 0 1, 0 0))'),
           struct_pack(xmin := 0.0, ymin := 0.0, xmax := 1.0, ymax := 1.0)),
          ('00000000-0000-0000-0000-000000000002', 'locality', 'Box', 1.0,
           ST_GeomFromText('POLYGON ((-0.1 0, 0.6 0, 0.6 1, -0.1 1, -0.1 0))'),
           struct_pack(xmin := -0.1, ymin := 0.0, xmax := 0.6, ymax := 1.0))
        ) AS t(division_id, subtype, "name", area_m2, geometry, bbox)
    """)
    connection.execute(f"COPY source_addresses TO '{addresses}' (FORMAT PARQUET)")
    connection.execute(f"COPY source_divisions TO '{divisions}' (FORMAT PARQUET)")
    connection.close()

    prepared = duckdb.connect()
    experiment.prepare_database(
        prepared, addresses, divisions, threads=2, memory_limit="1GB"
    )
    metrics = experiment.collect_metrics(prepared)
    chains = [
        row[0]
        for row in prepared.execute("SELECT chain_signature FROM enriched").fetchall()
    ]
    prepared.close()

    assert metrics["candidate_ambiguity"]["boundary_addresses"] == 1
    assert metrics["candidate_ambiguity"]["ambiguous_pairs"] == 2
    assert all(chain.count("locality:") == 2 for chain in chains)


def test_cli_requires_explicit_database_overwrite(tmp_path, monkeypatch):
    database = tmp_path / "existing.duckdb"
    database.write_bytes(b"do not replace")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            str(tmp_path / "addresses.parquet"),
            str(tmp_path / "divisions.parquet"),
            "--artifact",
            str(tmp_path / "artifact.aidx"),
            "--database",
            str(database),
            "--json-out",
            str(tmp_path / "report.json"),
            "--markdown-out",
            str(tmp_path / "report.md"),
        ],
    )
    with pytest.raises(SystemExit, match="--overwrite-database"):
        experiment.main()
    assert database.read_bytes() == b"do not replace"
