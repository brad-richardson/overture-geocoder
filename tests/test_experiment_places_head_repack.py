"""Hermetic tests for the single-object global-head repack."""

import importlib.util
import json
import sys
from pathlib import Path


SCRIPTS = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _load(name):
    script = SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, script)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


experiment = _load("experiment_places_head_repack")
locality = _load("experiment_places_locality_head")
compact = sys.modules["experiment_places_compact_index"]


def starbucks(count, prefix="sf", base_lat=37.77, base_lon=-122.42, conf0=0.99):
    rows = [
        {
            "id": f"{prefix}-{i}",
            "name": "Starbucks",
            "category": "cafe",
            "lat": base_lat + i / 10000,
            "lon": base_lon,
            "confidence": conf0 - i / 1000,
        }
        for i in range(count)
    ]
    return [compact.place_from_row(row, n) for n, row in enumerate(rows, 1)]


# --- pure encoding logic -------------------------------------------------


def test_key_index_round_trips():
    entries = [("e:alpha", 0, 12), ("e:beta", 12, 40), ("p:be", 52, 7)]
    encoded = experiment.encode_key_index(entries)
    decoded = experiment.decode_key_index(encoded)
    assert decoded == {"e:alpha": (0, 12), "e:beta": (12, 40), "p:be": (52, 7)}


def test_key_index_rejects_unsorted():
    try:
        experiment.encode_key_index([("e:beta", 0, 1), ("e:alpha", 1, 1)])
    except ValueError:
        return
    raise AssertionError("expected unsorted key index to raise")


def test_head_entry_round_trips_projection_fields():
    places = starbucks(3)
    records = [compact.encode_record(place) for place in places]
    entry = experiment.encode_head_entry(records)
    decoded = experiment.decode_head_entry(entry)
    assert [row["id"] for row in decoded] == ["sf-0", "sf-1", "sf-2"]
    assert decoded[0]["name"] == "Starbucks"


# --- reproduction fidelity ----------------------------------------------


def test_repack_reproduces_locality_head_object_exactly():
    places = starbucks(20)
    ordered, heads, store = experiment.build_heads_and_baseline(
        places, head_minimum_candidates=2
    )
    index = locality.LocalityHeadIndex(
        places, cell_degrees=experiment.CELL_DEGREES, head_minimum_candidates=2
    )
    assert heads == index.heads
    assert set(store.pages_by_key) == set(index.head_store.pages_by_key)
    assert len(store.pages) == len(index.head_store.pages)
    assert store.bucket_count == index.head_store.bucket_count


# --- reader models -------------------------------------------------------


def test_resident_hit_is_one_entry_read_and_cold_is_two(tmp_path):
    places = starbucks(20)
    ordered, heads, store = experiment.build_heads_and_baseline(
        places, head_minimum_candidates=2
    )
    obj = tmp_path / "head.repack"
    meta = experiment.build_repack_object(ordered, heads, obj)
    assert meta["objects"] == 1
    reader = experiment.RepackHead(obj)
    reader.load_resident_index()
    key = "e:starbucks"
    assert key in heads
    resident = reader.query_resident(key)
    cold = reader.query_cold(key)
    assert resident["hit"] and cold["hit"]
    assert resident["reads"] == 1
    assert cold["reads"] == 2
    # Resident transfers only the entry; cold adds the whole key index.
    assert resident["bytes"] < cold["bytes"]
    assert resident["result_ids"] == cold["result_ids"]


def test_miss_costs_zero_resident_reads_and_one_cold_index_read(tmp_path):
    places = starbucks(20)
    ordered, heads, store = experiment.build_heads_and_baseline(
        places, head_minimum_candidates=2
    )
    obj = tmp_path / "head.repack"
    experiment.build_repack_object(ordered, heads, obj)
    reader = experiment.RepackHead(obj)
    reader.load_resident_index()
    miss = "e:zzunlikelyheadtoken"
    assert miss not in heads
    resident = reader.query_resident(miss)
    cold = reader.query_cold(miss)
    assert resident == {"hit": False, "reads": 0, "bytes": 0, "result_ids": []}
    assert cold["hit"] is False and cold["reads"] == 1


def test_bucket_baseline_overfetches_relative_to_resident_entry(tmp_path):
    places = starbucks(20)
    ordered, heads, store = experiment.build_heads_and_baseline(
        places, head_minimum_candidates=2
    )
    obj = tmp_path / "head.repack"
    experiment.build_repack_object(ordered, heads, obj)
    reader = experiment.RepackHead(obj)
    reader.load_resident_index()
    baseline = experiment.BucketBaseline(store)
    key = "e:starbucks"
    base = baseline.lookup(key)
    resident = reader.query_resident(key)
    assert base["hit"] and base["reads"] == 1
    # A whole-bucket read is strictly larger than the single matched entry.
    assert base["bytes"] > resident["bytes"]


def test_query_set_classifies_hits_ineligible_and_miss():
    places = starbucks(20)
    _, heads, _ = experiment.build_heads_and_baseline(
        places, head_minimum_candidates=2
    )
    kinds = {q.name: q.kind for q in experiment.query_set(heads)}
    assert kinds["starbucks_exact"] == "hit"
    assert kinds["starbucks_long_prefix"] == "hit"
    assert kinds["hotel_category"] == "ineligible"
    assert kinds["golden_gate_prefix"] == "ineligible"
    assert kinds["absent_exact_miss"] == "eligible_miss"


def test_cli_writes_reports(tmp_path):
    places = starbucks(70)
    source = tmp_path / "places.jsonl"
    source.write_text(
        "\n".join(
            json.dumps(
                {
                    "id": p.place_id,
                    "name": p.name,
                    "category": p.category,
                    "lat": p.lat,
                    "lon": p.lon,
                    "confidence": p.confidence,
                }
            )
            for p in places
        )
    )
    obj = tmp_path / "head.repack"
    json_out = tmp_path / "report.json"
    md_out = tmp_path / "report.md"
    assert (
        experiment.main(
            [
                str(source),
                "--object-out",
                str(obj),
                "--json-out",
                str(json_out),
                "--markdown-out",
                str(md_out),
            ]
        )
        == 0
    )
    report = json.loads(json_out.read_text())
    assert report["single_object_head"]["objects"] == 1
    assert report["baseline_bucket_head"]["objects"] >= 1
    assert report["single_object_head"]["key_index_bytes"] > 0
    assert "single-object repack" in md_out.read_text()
