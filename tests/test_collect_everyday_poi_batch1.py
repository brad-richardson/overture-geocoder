"""Contract tests for deterministic Singapore/Taiwan POI collection."""

import hashlib
import importlib.util
import json
from pathlib import Path
import zipfile


ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "scripts" / "collect_everyday_poi_batch1.py"
spec = importlib.util.spec_from_file_location("collect_everyday_poi_batch1", SCRIPT)
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)


def source(source_id):
    return {
        "accessed_at": "2026-08-03T00:00:00Z",
        "id": source_id,
        "snapshot_sha256": "a" * 64,
        "source_license": "Open data licence",
        "source_name": "Official source",
        "source_url": "https://example.gov/source",
    }


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


def test_committed_batch_is_frozen_partial_evidence():
    payload_bytes = (ROOT / "benchmarks/everyday-poi-tripwire-cases-v1.json").read_bytes()
    payload = json.loads(payload_bytes)
    report = json.loads(
        (ROOT / "benchmarks/everyday-poi-selection-report-v1.json").read_text()
    )
    cases = payload["cases"]
    assert payload["schema"] == "benchmark-v2-forward-cases-v1"
    assert len(cases) == 50
    assert len({item["id"] for item in cases}) == 50
    assert {item["strata"]["country"] for item in cases} == {"SG", "TW"}
    assert sum(item["strata"]["country"] == "SG" for item in cases) == 20
    assert sum(item["strata"]["country"] == "TW" for item in cases) == 30
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
