"""Contract tests for deterministic everyday-POI source collection."""

import hashlib
import importlib.util
import json
from pathlib import Path
import zipfile


ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "scripts" / "collect_everyday_poi.py"
spec = importlib.util.spec_from_file_location("collect_everyday_poi", SCRIPT)
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)


def source(source_id):
    value = {
        "accessed_at": "2026-08-03T00:00:00Z",
        "id": source_id,
        "snapshot_sha256": "a" * 64,
        "source_license": "Open data licence",
        "source_name": "Official source",
        "source_url": "https://example.gov/source",
    }
    if source_id == "kr-seoul-hospital-licenses":
        value["coordinate_transformation"] = {
            "accuracy_metres": 1.0,
            "always_xy": True,
            "operation": "pinned test operation",
            "proj_version": "9.5.1",
            "pyproj_version": "3.7.2",
            "source_crs": "EPSG:5174",
            "target_crs": "EPSG:4326",
        }
    return value


def sg_feature(object_id, station, exit_code, lon=103.8, lat=1.3):
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {
            "OBJECTID": object_id,
            "STATION_NA": station,
            "EXIT_CODE": exit_code,
        },
    }


def restaurant(record_id, name, *, town="中西區", status=1, lon=120.2, lat=23.0):
    return {
        "RestaurantID": record_id,
        "RestaurantName": name,
        "PositionLat": lat,
        "PositionLon": lon,
        "PostalAddress": {"City": "臺南市", "Town": town},
        "ServiceStatus": status,
    }


def melbourne_record(record_id, name, *, code="4121", lon=144.96, lat=-37.81):
    return {
        "recordid": record_id,
        "fields": {
            "business_address": "1 Test Street MELBOURNE VIC 3000",
            "census_year": "2024",
            "industry_anzsic4_code": code,
            "industry_anzsic4_description": "Fresh Meat, Fish and Poultry Retailing",
            "latitude": lat,
            "longitude": lon,
            "property_id": "1",
            "trading_name": name,
        },
    }


def bogota_feature(record_id, object_id, name, *, provider="Pública", geometry=True):
    return {
        "type": "Feature",
        "geometry": (
            {"type": "Point", "coordinates": [-74.07, 4.65]}
            if geometry
            else None
        ),
        "properties": {
            "CLASE_DE_P": "Instituciones Prestadoras de Servicios de Salud - IPS",
            "DIRECCION": "Calle 1",
            "ID": record_id,
            "NOMBRE_DE_": "SUBRED TEST",
            "NOMBRE_DEL": name,
            "OBJECTID": object_id,
            "TIPO_DE_PR": provider,
        },
    }


def test_selection_digest_follows_frozen_nul_contract():
    expected = hashlib.sha256(b"plan\0record").hexdigest()
    assert collector.selection_digest("plan", "record") == expected


def test_singapore_collapses_exits_before_deterministic_station_selection(tmp_path):
    payload = {
        "type": "FeatureCollection",
        "features": [
            sg_feature(3, "ALPHA MRT STATION", "Exit 10"),
            sg_feature(2, "ALPHA MRT STATION", "Exit 2"),
            sg_feature(1, "BETA MRT STATION", "Exit A"),
        ],
    }
    path = tmp_path / "sg.geojson"
    path.write_text(json.dumps(payload))
    cases, report = collector.collect_singapore(
        path, source("sg-lta-mrt-exits"), quota=2
    )
    alpha = next(item for item in cases if item["expected_name"].startswith("ALPHA"))
    assert alpha["provenance"]["source_record_id"] == "2"
    assert report["input_features"] == 3
    assert report["eligible_unique_stations"] == 2
    assert report["filter_counts"] == {"collapsed_additional_station_exit": 1}
    assert report["review_exclusions"] == []


def test_taiwan_filters_to_active_core_points_and_retains_duplicate_exclusions(
    tmp_path,
):
    restaurants = [
        restaurant("one", "甲餐廳"),
        restaurant("two", "重名餐廳"),
        restaurant("three", " 重名餐廳 "),
        restaurant("four", "郊區餐廳", town="新營區"),
        restaurant("five", "停業餐廳", status=0),
    ]
    path = tmp_path / "tw.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("RestaurantList.json", json.dumps({"Restaurants": restaurants}))
    cases, report = collector.collect_taiwan(
        path, source("tw-tourism-restaurants"), quota=1
    )
    assert [item["expected_name"] for item in cases] == ["甲餐廳"]
    assert report["eligible_before_duplicate_filter"] == 3
    assert report["eligible_after_duplicate_filter"] == 1
    assert report["filter_counts"] == {
        "not_active": 1,
        "outside_tainan_core_districts": 1,
    }
    assert report["review_exclusions"] == [
        {"reason": "duplicate_official_name", "source_record_id": "two"},
        {"reason": "duplicate_official_name", "source_record_id": "three"},
    ]


def test_javascript_data_parser_accepts_only_the_seoul_data_subset():
    value = collector.JavaScriptDataParser(
        '{result:"ok",page:{totalCount:1,listCount:1},'
        'list:[{MGTNO:"one",BPLCNM:"name,field: still string",X:"1",Y:"2"},]}'
    ).parse()
    assert value["list"][0]["BPLCNM"] == "name,field: still string"
    for unsafe in ('{value:alert(1)}', '{value:`template`}', '{value:undefined}'):
        try:
            collector.JavaScriptDataParser(unsafe).parse()
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe JavaScript was accepted: {unsafe}")


def test_hong_kong_selects_bilingual_points_and_retains_english_alias(tmp_path):
    payload = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [114.1, 22.3]},
                "properties": {
                    "OBJECTID": 7,
                    "NAME_TC": "測試醫院",
                    "NAME_EN": "Test Hospital",
                },
            }
        ],
    }
    path = tmp_path / "hk.geojson"
    path.write_text(json.dumps(payload))
    cases, report = collector.collect_hong_kong(
        path, source("hk-ha-health-care-facilities"), quota=1
    )
    assert cases[0]["expected_name"] == "測試醫院"
    assert cases[0]["alt_names"] == ["Test Hospital"]
    assert report["eligible_after_duplicate_filter"] == 1


def test_seoul_filters_active_rows_and_retains_transform_provenance(
    tmp_path, monkeypatch
):
    path = tmp_path / "seoul.js"
    path.write_text(
        '{result:"ok",page:{totalCount:2,listCount:2},list:['
        '{TRDSTATEGBN:"01",DTLSTATENM:"영업중",BPLCNM:"테스트병원",'
        'MGTNO:"active",X:"200000",Y:"450000"},'
        '{TRDSTATEGBN:"03",DTLSTATENM:"폐업",BPLCNM:"닫힌병원",'
        'MGTNO:"closed",X:"200000",Y:"450000"},]}'
    )

    class Transformer:
        def transform(self, x, y):
            assert (x, y) == (200000.0, 450000.0)
            return 127.0, 37.5

    monkeypatch.setattr(collector, "seoul_transformer", lambda _: Transformer())
    cases, report = collector.collect_seoul(
        path, source("kr-seoul-hospital-licenses"), quota=1
    )
    assert cases[0]["expected_name"] == "테스트병원"
    assert cases[0]["provenance"]["source_coordinate"] == {
        "x": "200000",
        "y": "450000",
        "crs": "EPSG:5174",
    }
    assert report["filter_counts"] == {"not_active_trade_state": 1}


def test_melbourne_filters_physical_retail_and_retains_review_exclusion(
    tmp_path, monkeypatch
):
    records = [
        melbourne_record("b", "Public Shop"),
        melbourne_record("a", "Storage Only"),
        melbourne_record("service", "Service", code="4310"),
        melbourne_record("duplicate-one", "Duplicate Shop"),
        melbourne_record("duplicate-two", " duplicate shop "),
    ]
    path = tmp_path / "melbourne.json"
    path.write_text(json.dumps(records))
    monkeypatch.setattr(
        collector,
        "MELBOURNE_REVIEW_EXCLUSIONS",
        {"a": "storage_only_not_public_retail"},
    )
    cases, report = collector.collect_melbourne(
        path, source("au-melbourne-clue-businesses"), quota=1
    )
    assert [item["expected_name"] for item in cases] == ["Public Shop"]
    assert cases[0]["provenance"]["industry_anzsic4_code"] == "4121"
    assert report["eligible_before_duplicate_filter"] == 4
    assert report["eligible_after_duplicate_filter"] == 2
    assert report["filter_counts"] == {"outside_physical_retail_divisions": 1}
    assert {item["reason"] for item in report["review_exclusions"]} == {
        "duplicate_official_name",
        "storage_only_not_public_retail",
    }


def test_bogota_filters_public_valid_points_and_uses_stable_id(tmp_path):
    payload = {
        "type": "FeatureCollection",
        "features": [
            bogota_feature(2475, 1, "Unidad de Salud"),
            bogota_feature(2476, 2, "Private Facility", provider="Privada"),
            bogota_feature(2477, 3, "Missing Point", geometry=False),
        ],
    }
    path = tmp_path / "bogota.geojson"
    path.write_text(json.dumps(payload))
    cases, report = collector.collect_bogota(
        path, source("co-bogota-public-health-network"), quota=1
    )
    assert cases[0]["id"] == "everyday-co-2475"
    assert cases[0]["provenance"]["source_object_id"] == "1"
    assert report["filter_counts"] == {
        "invalid_point": 1,
        "not_public_provider": 1,
    }


def test_committed_batch_is_frozen_partial_evidence():
    payload_bytes = (ROOT / "benchmarks/everyday-poi-tripwire-cases-v1.json").read_bytes()
    payload = json.loads(payload_bytes)
    report = json.loads(
        (ROOT / "benchmarks/everyday-poi-selection-report-v1.json").read_text()
    )
    cases = payload["cases"]
    assert payload["schema"] == "benchmark-v2-forward-cases-v1"
    assert len(cases) == 135
    assert len({item["id"] for item in cases}) == 135
    assert {item["strata"]["country"] for item in cases} == {
        "AU",
        "CO",
        "HK",
        "KR",
        "SG",
        "TW",
    }
    assert sum(item["strata"]["country"] == "SG" for item in cases) == 20
    assert sum(item["strata"]["country"] == "TW" for item in cases) == 30
    assert sum(item["strata"]["country"] == "HK" for item in cases) == 20
    assert sum(item["strata"]["country"] == "KR" for item in cases) == 20
    assert sum(item["strata"]["country"] == "CO" for item in cases) == 20
    assert sum(item["strata"]["country"] == "AU" for item in cases) == 25
    assert all(item["selection_review"]["decision"] == "accepted" for item in cases)
    assert all("expected_gers_id" not in item for item in cases)
    assert report["provider_requests_made_during_selection"] == 0
    assert report["case_output_sha256"] == hashlib.sha256(payload_bytes).hexdigest()


def test_snapshot_manifest_has_exact_hashes_and_licence_evidence():
    manifest = json.loads(
        (ROOT / "benchmarks/everyday-poi-source-snapshots-v1.json").read_text()
    )
    assert manifest["schema"] == "everyday-poi-source-snapshots-v1"
    assert {item["id"] for item in manifest["sources"]} == {
        "sg-lta-mrt-exits",
        "tw-tourism-restaurants",
        "hk-ha-health-care-facilities",
        "kr-seoul-hospital-licenses",
        "co-bogota-public-health-network",
        "au-melbourne-clue-businesses",
    }
    for item in manifest["sources"]:
        assert len(item["snapshot_sha256"]) == 64
        assert len(item["license_snapshot_sha256"]) == 64
        assert item["snapshot_bytes"] > 0
        assert item["license_snapshot_bytes"] > 0
        assert item["source_url"].startswith("https://")
        assert item["license_url"].startswith("https://")
        snapshot = ROOT / item["snapshot_path"]
        licence = ROOT / item["license_snapshot_path"]
        assert snapshot.stat().st_size == item["snapshot_bytes"]
        assert licence.stat().st_size == item["license_snapshot_bytes"]
        assert collector.sha256_file(snapshot) == item["snapshot_sha256"]
        assert collector.sha256_file(licence) == item["license_snapshot_sha256"]
        for metadata in item.get("metadata_snapshots", []):
            metadata_path = ROOT / metadata["path"]
            assert metadata_path.stat().st_size == metadata["bytes"]
            assert collector.sha256_file(metadata_path) == metadata["sha256"]


def test_committed_seoul_preview_is_complete_and_status_explicit():
    rows = collector.parse_seoul_preview(
        ROOT / "benchmarks/everyday-poi-source-data-v1/kr-seoul-hospitals.js"
    )
    assert len(rows) == 929
    assert sum(
        row["TRDSTATEGBN"] == "01" and row["DTLSTATENM"] == "영업중"
        for row in rows
    ) == 555
    assert len({row["MGTNO"] for row in rows}) == 929


def test_committed_melbourne_snapshot_has_stable_record_ids():
    records = json.loads(
        (
            ROOT
            / "benchmarks/everyday-poi-source-data-v1/au-melbourne-businesses-2024.json"
        ).read_text()
    )
    assert len(records) == 19672
    assert len({item["recordid"] for item in records}) == 19672


def test_committed_bogota_snapshot_has_stable_ids_and_116_points():
    payload = json.loads(
        (
            ROOT
            / "benchmarks/everyday-poi-source-data-v1/co-bogota-health-network.geojson"
        ).read_text()
    )
    features = payload["features"]
    assert len(features) == 117
    assert len({item["properties"]["ID"] for item in features}) == 117
    assert len({item["properties"]["OBJECTID"] for item in features}) == 117
    assert sum(
        (item.get("geometry") or {}).get("type") == "Point"
        and collector.valid_point((item.get("geometry") or {}).get("coordinates"))
        for item in features
    ) == 116
