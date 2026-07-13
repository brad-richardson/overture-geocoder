import json
import sys
from pathlib import Path

import duckdb
import pytest


SCRIPTS = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import download_divisions_smoke as smoke  # noqa: E402
import verify_monaco_export as verify  # noqa: E402
import verify_monaco_evidence as evidence  # noqa: E402


def division_rows(contract):
    subtype_by_id = {
        item["id"]: item["subtype"] for item in contract["required_divisions"]
    }
    subtype_by_id.update(
        {item["division_id"]: item["subtype"] for item in contract["required_areas"]}
    )
    ids = sorted(subtype_by_id)
    hierarchy = json.dumps(
        [[{"division_id": division_id, "subtype": "test"} for division_id in ids]]
    )
    return [
        {
            "id": item["id"],
            "subtype": item["subtype"],
            "country": "MC",
            "region": None,
            "name": "Monaco",
            "population": 50_000,
            "parent_division_id": None,
            "hierarchies_json": hierarchy,
            "xmin": 7.4,
            "ymin": 43.7,
            "xmax": 7.5,
            "ymax": 43.8,
            "lon": 7.45,
            "lat": 43.75,
            "wkb_sha256": "division-geometry",
        }
        for item in (
            {"id": division_id, "subtype": subtype_by_id[division_id]}
            for division_id in ids
        )
    ]


def area_rows(contract):
    return [
        {
            "id": item["id"],
            "division_id": item["division_id"],
            "subtype": item["subtype"],
            "country": "MC",
            "xmin": 7.3,
            "ymin": 43.5,
            "xmax": 7.6,
            "ymax": 43.9,
            "geom_xmin": 7.3,
            "geom_ymin": 43.5,
            "geom_xmax": 7.6,
            "geom_ymax": 43.9,
            "geometry_area": 0.01,
            "wkb_sha256": "area-geometry",
        }
        for item in contract["required_areas"]
    ]


def test_contract_and_required_closure_validate():
    contract = smoke.load_contract()
    ids, boxes = smoke.validate_divisions(division_rows(contract), contract)
    assert ids.issuperset({item["id"] for item in contract["required_divisions"]})
    boxes, signatures = smoke.validate_areas(area_rows(contract), contract, ids)
    assert boxes
    assert sum(signatures.values()) == len(contract["required_areas"])
    assert boxes


def test_missing_required_division_fails_closed():
    contract = smoke.load_contract()
    missing_id = contract["required_divisions"][-1]["id"]
    with pytest.raises(RuntimeError, match="required Monaco divisions are missing"):
        smoke.validate_divisions(
            [row for row in division_rows(contract) if row["id"] != missing_id],
            contract,
        )


def test_foreign_bbox_hit_is_rejected():
    contract = smoke.load_contract()
    rows = division_rows(contract)
    rows[0]["country"] = "FR"
    with pytest.raises(RuntimeError, match="escaped country MC"):
        smoke.validate_divisions(rows, contract)


def test_unresolved_hierarchy_id_fails_closed():
    contract = smoke.load_contract()
    rows = division_rows(contract)
    rows[0]["hierarchies_json"] = json.dumps(
        [[{"division_id": "missing-parent", "subtype": "region"}]]
    )
    with pytest.raises(RuntimeError, match="hierarchy closure is unresolved"):
        smoke.validate_divisions(rows, contract)


def test_invalid_area_bbox_fails():
    contract = smoke.load_contract()
    ids, _ = smoke.validate_divisions(division_rows(contract), contract)
    rows = area_rows(contract)
    rows[0]["xmax"] = 7.2
    with pytest.raises(RuntimeError, match="invalid bbox"):
        smoke.validate_areas(rows, contract, ids)


def test_area_bbox_must_cover_geometry():
    contract = smoke.load_contract()
    ids, _ = smoke.validate_divisions(division_rows(contract), contract)
    rows = area_rows(contract)
    rows[0]["geom_xmax"] = 7.7
    with pytest.raises(RuntimeError, match="does not cover its geometry"):
        smoke.validate_areas(rows, contract, ids)


def test_area_country_mismatch_is_retained_by_division_closure():
    contract = smoke.load_contract()
    ids, _ = smoke.validate_divisions(division_rows(contract), contract)
    rows = area_rows(contract)
    rows[0]["country"] = None
    boxes, signatures = smoke.validate_areas(rows, contract, ids)
    assert boxes
    assert sum(signatures.values()) == len(contract["required_areas"])


def test_required_area_parent_with_foreign_country_is_in_closure():
    contract = smoke.load_contract()
    required_parent = contract["required_areas"][-1]["division_id"]
    assert required_parent in smoke._required_division_ids(contract)
    rows = division_rows(contract)
    by_id = {row["id"]: row for row in rows}
    by_id[required_parent]["country"] = "FR"
    core_ids = set(by_id) - {required_parent}
    ids, _ = smoke.validate_divisions(rows, contract, core_ids)
    areas = area_rows(contract)
    next(
        row for row in areas if row["division_id"] == required_parent
    )["country"] = None
    smoke.validate_areas(areas, contract, ids)


def test_area_filter_owns_unpinned_null_country_area_by_division():
    division_id = "validated-parent"
    predicate = smoke._country_or_ids_filter(
        "a.country", "a.division_id", {division_id}
    )
    assert "a.country = 'MC'" in predicate
    assert f"a.division_id IN ('{division_id}')" in predicate

    discovery = smoke._ownership_filter({division_id})
    assert "bbox" not in discovery
    missing = smoke._validate_ownership_rows(
        [
            {
                "id": "new-null-country-area",
                "division_id": division_id,
                "country": None,
                "xmin": -120.0,
                "ymin": 10.0,
                "xmax": -119.0,
                "ymax": 11.0,
            }
        ],
        {division_id},
        set(),
    )
    assert missing == {"new-null-country-area"}


def test_output_validation_enforces_multipart_component_multiplicity(tmp_path):
    contract = smoke.load_contract()
    forward = tmp_path / "forward.parquet"
    reverse = tmp_path / "reverse.parquet"
    con = duckdb.connect()
    try:
        required = contract["required_divisions"]
        con.execute(
            "CREATE TABLE forward(gers_id VARCHAR, subtype VARCHAR, country VARCHAR, "
            "search_name VARCHAR, search_context VARCHAR)"
        )
        con.executemany(
            "INSERT INTO forward VALUES (?, ?, 'MC', 'monaco', 'monaco mc')",
            [(item["id"], item["subtype"]) for item in required],
        )
        con.execute(f"COPY forward TO '{forward}' (FORMAT PARQUET)")
        con.execute(
            "CREATE TABLE reverse(gers_id VARCHAR, subtype VARCHAR, country VARCHAR, "
            "bbox_xmin DOUBLE, bbox_ymin DOUBLE, bbox_xmax DOUBLE, bbox_ymax DOUBLE, "
            "area DOUBLE)"
        )
        country_id = required[0]["id"]
        region_id = required[1]["id"]
        con.execute(
            "INSERT INTO reverse VALUES "
            "(?, 'country', 'MC', 1, 2, 3, 4, 5), "
            "(?, 'region', 'MC', 6, 7, 8, 9, 10)",
            [country_id, region_id],
        )
        con.execute(f"COPY reverse TO '{reverse}' (FORMAT PARQUET)")
    finally:
        con.close()
    expected = smoke.collections.Counter(
        {
            (country_id, "country", 1.0, 2.0, 3.0, 4.0, 5.0): 2,
            (region_id, "region", 6.0, 7.0, 8.0, 9.0, 10.0): 1,
        }
    )
    with pytest.raises(RuntimeError, match="component multiplicity"):
        smoke._validate_outputs(forward, reverse, contract, expected)


def test_envelope_is_derived_from_all_components():
    envelope = smoke.conservative_envelope(
        [(7.4, 43.7, 7.5, 43.8), (7.1, 43.2, 7.8, 44.0)]
    )
    assert envelope[0] < 7.1
    assert envelope[1] < 43.2
    assert envelope[2] > 7.8
    assert envelope[3] > 44.0


def test_renderer_rejects_unexpanded_placeholder(tmp_path):
    template = tmp_path / "template.sql"
    template.write_text("SELECT '__ONE__', '__TWO__'")
    with pytest.raises(RuntimeError, match="unrendered SQL placeholders"):
        smoke.render_sql(template, {"__ONE__": "one"})


def test_production_templates_render_global_mode_without_placeholders():
    forward = smoke.render_sql(
        smoke.DIVISION_TEMPLATE,
        {
            "__OVERTURE_RELEASE__": "2026-06-17.0",
            "__DIVISION_FILTER__": "TRUE",
            "__OUTPUT_PATH__": "forward.parquet",
        },
    )
    reverse = smoke.render_sql(
        smoke.AREA_TEMPLATE,
        {
            "__OVERTURE_RELEASE__": "2026-06-17.0",
            "__DIVISION_FILTER__": "TRUE",
            "__AREA_FILTER__": "TRUE",
            "__OUTPUT_PATH__": "reverse.parquet",
        },
    )
    assert "__" not in forward
    assert "__" not in reverse
    assert forward.count("AND (TRUE)") == 3
    assert reverse.count("AND (TRUE)") == 2


def _write_parquet(path: Path, rows):
    con = duckdb.connect()
    try:
        con.execute("CREATE TABLE rows(id VARCHAR, country VARCHAR, value INTEGER)")
        con.executemany("INSERT INTO rows VALUES (?, ?, ?)", rows)
        con.execute(f"COPY rows TO '{path}' (FORMAT PARQUET)")
    finally:
        con.close()


def test_equivalence_compares_schema_rows_and_multiplicity(tmp_path):
    legacy_forward = tmp_path / "legacy-forward.parquet"
    legacy_reverse = tmp_path / "legacy-reverse.parquet"
    subset_forward = tmp_path / "subset-forward.parquet"
    subset_reverse = tmp_path / "subset-reverse.parquet"
    legacy_rows = [("mc", "MC", 1), ("mc", "MC", 1), ("fr", "FR", 2)]
    subset_rows = [("mc", "MC", 1), ("mc", "MC", 1)]
    for path, rows in (
        (legacy_forward, legacy_rows),
        (legacy_reverse, legacy_rows),
        (subset_forward, subset_rows),
        (subset_reverse, subset_rows),
    ):
        _write_parquet(path, rows)
    report = verify.verify_exports(
        legacy_forward, legacy_reverse, subset_forward, subset_reverse
    )
    assert report["forward"]["rows"] == 2

    _write_parquet(subset_reverse, [("mc", "MC", 1)])
    with pytest.raises(RuntimeError, match="reverse Monaco subset drifted"):
        verify.verify_exports(
            legacy_forward, legacy_reverse, subset_forward, subset_reverse
        )


def test_committed_monaco_evidence_is_current():
    report = evidence.verify_evidence(
        Path(__file__).parent.parent
        / "docs/plans/2026-07-12-monaco-subset-evidence.json"
    )
    assert report["logical_equivalence_to_no_bbox_baseline"]["built_shards"][
        "router"
    ]["logical_contents_equal"]
