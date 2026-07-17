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


# --- famous-unique admission ---------------------------------------------


def famous_mix(count=20):
    rows = [
        {
            "id": f"sf-{i}",
            "name": "Starbucks",
            "category": "cafe",
            "lat": 37.77 + i / 10000,
            "lon": -122.42,
            "confidence": 0.99 - i / 1000,
        }
        for i in range(count)
    ]
    rows.append(
        {
            "id": "famous-tower",
            "name": "Coit Tower",
            "category": "landmark",
            "lat": 37.8024,
            "lon": -122.4058,
            # The highest quantized confidence, so the place is always inside
            # the famous cap regardless of tie-breaks.
            "confidence": 0.995,
        }
    )
    return [compact.place_from_row(row, n) for n, row in enumerate(rows, 1)]


def test_famous_pair_key_shapes():
    Clause = experiment.Clause
    assert (
        experiment.famous_pair_key((Clause("tower"), Clause("tokyo")))
        == "e2:tokyo tower"
    )
    assert (
        experiment.famous_pair_key((Clause("tokyo"), Clause("tower")))
        == "e2:tokyo tower"
    )
    assert experiment.famous_pair_key((Clause("tokyo"),)) is None
    assert experiment.famous_pair_key((Clause("tokyo"), Clause("tokyo"))) is None
    assert (
        experiment.famous_pair_key((Clause("tokyo"), Clause("tower", prefix=True)))
        is None
    )
    assert (
        experiment.famous_pair_key((Clause("tokyo"), Clause("tower", field="name")))
        is None
    )


def test_directory_provenance_and_family_split(tmp_path):
    places = famous_mix()
    ordered, heads, _ = experiment.build_heads_and_baseline(
        places, head_minimum_candidates=2, head_famous_cap=4
    )
    obj = tmp_path / "head.repack"
    meta = experiment.build_repack_object(ordered, heads, obj, head_famous_cap=4)
    reader = experiment.RepackHead(obj)
    e2_keys = [key for key in heads if key.startswith("e2:")]
    assert reader.directory["schema_version"] == 1
    assert reader.directory["head_famous_cap"] == 4
    assert reader.directory["admission"] == "famous-unique-v1"
    assert reader.directory["e2_key_count"] == len(e2_keys) > 0
    assert meta["key_counts_by_family"]["e2"] == len(e2_keys)
    assert meta["key_counts_by_family"]["e"] == sum(
        1 for key in heads if key.startswith("e:")
    )
    assert meta["entry_bytes_by_family"]["e2"] > 0
    assert sum(meta["entry_bytes_by_family"].values()) == meta["entries_bytes"]


def test_key_index_orders_e2_before_e_and_round_trips(tmp_path):
    places = famous_mix()
    ordered, heads, _ = experiment.build_heads_and_baseline(
        places, head_minimum_candidates=2, head_famous_cap=4
    )
    obj = tmp_path / "head.repack"
    experiment.build_repack_object(ordered, heads, obj, head_famous_cap=4)
    reader = experiment.RepackHead(obj)
    index = reader.load_resident_index()
    keys = list(index)
    assert keys == sorted(keys)
    assert set(keys) == set(heads)
    first_e = next(number for number, key in enumerate(keys) if key.startswith("e:"))
    assert all(not key.startswith("e2:") for key in keys[first_e:])


def test_two_famous_builds_are_byte_identical_and_baseline_entries_unchanged(
    tmp_path,
):
    places = famous_mix()
    ordered, heads, _ = experiment.build_heads_and_baseline(
        places, head_minimum_candidates=2, head_famous_cap=4
    )
    first = tmp_path / "head-a.repack"
    second = tmp_path / "head-b.repack"
    experiment.build_repack_object(ordered, heads, first, head_famous_cap=4)
    experiment.build_repack_object(ordered, heads, second, head_famous_cap=4)
    assert experiment.sha256_file(first) == experiment.sha256_file(second)

    baseline_ordered, baseline_heads, _ = experiment.build_heads_and_baseline(
        places, head_minimum_candidates=2
    )
    baseline_obj = tmp_path / "head-baseline.repack"
    experiment.build_repack_object(baseline_ordered, baseline_heads, baseline_obj)
    famous_reader = experiment.RepackHead(first)
    baseline_reader = experiment.RepackHead(baseline_obj)
    # A cap-0 build keeps the historical directory shape: no famous fields.
    for field in ("head_famous_cap", "e2_key_count", "admission"):
        assert field not in baseline_reader.directory
    famous_index = famous_reader.load_resident_index()
    famous_entries_base, _ = famous_reader.component("entries")
    baseline_entries_base, _ = baseline_reader.component("entries")
    for key, (offset, length) in baseline_reader.load_resident_index().items():
        famous_offset, famous_length = famous_index[key]
        assert famous_reader._read(
            famous_entries_base + famous_offset, famous_length
        ) == baseline_reader._read(baseline_entries_base + offset, length)


def test_build_fails_when_exceeding_reader_hard_caps(tmp_path, monkeypatch):
    places = famous_mix()
    ordered, heads, _ = experiment.build_heads_and_baseline(
        places, head_minimum_candidates=2, head_famous_cap=4
    )
    monkeypatch.setattr(experiment, "READER_MAX_HEAD_KEYS", 2)
    try:
        experiment.build_repack_object(
            ordered, heads, tmp_path / "head.repack", head_famous_cap=4
        )
    except ValueError as error:
        assert "hard caps" in str(error)
    else:
        raise AssertionError("expected an over-cap build to fail")


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
