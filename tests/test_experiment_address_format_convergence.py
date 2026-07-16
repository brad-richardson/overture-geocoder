from __future__ import annotations

import gzip
import importlib.util
import sys
import uuid
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location(
    "experiment_address_format_convergence",
    SCRIPTS / "experiment_address_format_convergence.py",
)
assert SPEC and SPEC.loader
convergence = importlib.util.module_from_spec(SPEC)
# Register before exec so @dataclass can resolve the module by name.
sys.modules[SPEC.name] = convergence
SPEC.loader.exec_module(convergence)


def row(**overrides):
    base = {
        "country": "US",
        "has_address_levels": True,
        "al_region": "MA",
        "al_locality": "Cambridge",
        "postal_city": "Cambridge",
        "cont_any": True,
        "cont_country": "US",
        "cont_region": "Massachusetts",
        "cont_county": "Middlesex County",
        "cont_locality": "Cambridge",
        "cont_neighborhood": "",
        "finer_names": [],
    }
    base.update(overrides)
    return base


# ----------------------------------------------------------------------------
# Taxonomy classification.
# ----------------------------------------------------------------------------
def test_missing_address_levels_takes_priority():
    assert (
        convergence.classify_agreement(row(has_address_levels=False, al_locality=""))
        == "missing_address_levels"
    )


def test_point_outside_any_division():
    assert (
        convergence.classify_agreement(
            row(cont_any=False, cont_region="", cont_county="", cont_locality="")
        )
        == "point_outside_any_division"
    )


def test_country_disagreement_is_gated_before_locality():
    assert (
        convergence.classify_agreement(row(country="US", cont_country="CA"))
        == "country_disagreement"
    )


def test_exact_agreement_requires_raw_equality():
    assert (
        convergence.classify_agreement(
            row(al_locality="Cambridge", cont_locality="Cambridge")
        )
        == "exact_agreement"
    )


def test_normalization_only_difference():
    assert (
        convergence.classify_agreement(
            row(al_locality="CAMBRIDGE ", cont_locality="Cambridge")
        )
        == "normalization_only"
    )


def test_postal_city_explains_locality_disagreement():
    # address_levels most-specific is a neighborhood, containment resolves the
    # municipality, and postal_city matches the containment municipality.
    result = convergence.classify_agreement(
        row(
            al_locality="East Cambridge",
            postal_city="Cambridge",
            cont_locality="Cambridge",
        )
    )
    assert result == "postal_city_vs_containment"


def test_unresolved_disagreement_when_nothing_reconciles():
    result = convergence.classify_agreement(
        row(
            al_locality="Somerville",
            postal_city="Somerville",
            cont_locality="Cambridge",
        )
    )
    assert result == "unresolved_disagreement"


def test_finer_granularity_neighborhood_polygon_is_not_a_conflict():
    # USPS-style uppercase neighborhood label inside the Boston municipality
    # polygon: the containing neighborhood polygon explains the label.
    result = convergence.classify_agreement(
        row(
            al_locality="BRIGHTON",
            postal_city="",
            cont_locality="Boston",
            cont_neighborhood="Brighton",
        )
    )
    assert result == "finer_granularity_neighborhood"


def test_finer_granularity_division_point_name_is_not_a_conflict():
    # Overture has no Brighton polygon, only a macrohood division point inside
    # the Boston municipality; the point-name channel explains the label.
    result = convergence.classify_agreement(
        row(
            al_locality="CHARLESTOWN",
            postal_city="",
            cont_locality="Boston",
            cont_neighborhood="",
            finer_names=["Allston", "Brighton", "Charlestown"],
        )
    )
    assert result == "finer_granularity_neighborhood"


def test_locality_agreement_wins_over_neighborhood_match():
    # If the label already agrees with the municipality, the neighborhood
    # channel must not reclassify it.
    result = convergence.classify_agreement(
        row(
            al_locality="BOSTON",
            cont_locality="Boston",
            cont_neighborhood="Brighton",
        )
    )
    assert result == "normalization_only"


def test_neighborhood_mismatch_stays_unresolved():
    result = convergence.classify_agreement(
        row(
            al_locality="SOMERVILLE",
            postal_city="",
            cont_locality="Cambridge",
            cont_neighborhood="Brighton",
        )
    )
    assert result == "unresolved_disagreement"


def test_falls_back_to_region_when_no_locality_containment():
    # No locality polygon; the most-specific containment name is the region.
    assert (
        convergence.classify_agreement(
            row(
                al_locality="",
                al_region="Massachusetts",
                cont_locality="",
                cont_county="",
                cont_region="Massachusetts",
            )
        )
        == "exact_agreement"
    )


def test_taxonomy_counts_cover_every_bucket_and_sum_to_total():
    rows = [
        row(),  # exact_agreement
        row(al_locality="CAMBRIDGE"),  # normalization_only
        row(
            al_locality="BRIGHTON",
            postal_city="",
            cont_locality="Boston",
            cont_neighborhood="Brighton",
        ),  # finer_granularity_neighborhood
        row(al_locality="East Cambridge"),  # postal_city_vs_containment
        row(has_address_levels=False),  # missing_address_levels
        row(
            cont_any=False, cont_region="", cont_county="", cont_locality=""
        ),  # outside
        row(cont_country="CA"),  # country_disagreement
        row(al_locality="Somerville", postal_city="Somerville"),  # unresolved
    ]
    counts = convergence.taxonomy_counts(rows)
    assert set(counts) == set(convergence.TAXONOMY)
    assert sum(counts.values()) == len(rows)
    assert counts["exact_agreement"] == 1
    assert counts["normalization_only"] == 1
    assert counts["finer_granularity_neighborhood"] == 1
    assert counts["postal_city_vs_containment"] == 1
    assert counts["missing_address_levels"] == 1
    assert counts["point_outside_any_division"] == 1
    assert counts["country_disagreement"] == 1
    assert counts["unresolved_disagreement"] == 1


# ----------------------------------------------------------------------------
# Match byte packing.
# ----------------------------------------------------------------------------
def test_match_byte_round_trips_every_nibble_pair():
    for method in range(16):
        for confidence in range(16):
            packed = convergence.encode_match_byte(method, confidence)
            assert convergence.decode_match_byte(packed) == (method, confidence)


def test_match_byte_rejects_out_of_range():
    with pytest.raises(ValueError):
        convergence.encode_match_byte(16, 0)
    with pytest.raises(ValueError):
        convergence.encode_match_byte(0, 16)
    with pytest.raises(ValueError):
        convergence.decode_match_byte(256)


# ----------------------------------------------------------------------------
# Division-extension codec (page dictionary + per-row index + match byte).
# ----------------------------------------------------------------------------
def ext_record(ids, method, confidence):
    return {
        "division_gers_ids": [str(uuid.UUID(int=i)) for i in ids],
        "match_method": method,
        "match_confidence": confidence,
    }


def test_division_extension_round_trips_and_dedupes_dictionary():
    records = [
        ext_record([1, 2], convergence.MATCH_METHOD_INTERIOR, 3),
        ext_record([2, 3], convergence.MATCH_METHOD_BOUNDARY, 1),
        ext_record([], convergence.MATCH_METHOD_NONE, 0),
    ]
    payload = convergence.encode_division_extension(records)
    decoded, position = convergence.decode_division_extension(payload, len(records))
    assert position == len(payload)
    for original, back in zip(records, decoded):
        assert set(back["division_gers_ids"]) == set(original["division_gers_ids"])
        assert back["match_method"] == original["match_method"]
        assert back["match_confidence"] == original["match_confidence"]
    # Dictionary holds three distinct UUIDs (1, 2, 3) though four references exist.
    assert payload[0] == 3


def extended_page_fixture():
    return [
        {
            "key": (
                "us",
                "ma",
                "cambridge",
                "cambridge",
                "02139",
                "main street",
                "10",
                "",
                str(uuid.UUID(int=7)),
            ),
            "id": str(uuid.UUID(int=7)),
            "lon": -71.1,
            "lat": 42.37,
            "source_object_index": 0,
            "source_row_group": 0,
            "source_row_index": 0,
            "country": "US",
            "postal_city": "Cambridge",
            "postcode": "02139",
            "street": "Main Street",
            "number": "10",
            "unit": "",
            "address_levels": ["MA", "Cambridge"],
            "division_gers_ids": [str(uuid.UUID(int=7)), str(uuid.UUID(int=8))],
            "match_method": convergence.MATCH_METHOD_INTERIOR,
            "match_confidence": 2,
        }
    ]


def test_stored_extended_page_decodes_with_no_out_of_band_knowledge():
    # The stored blob is gzip(uvarint core length + core + extension), exactly
    # as measure_storage stores it. A reader must recover both halves from the
    # bytes alone -- no external core length, row count, or offsets.
    page = extended_page_fixture()
    stored = gzip.compress(convergence.encode_extended_page(page), mtime=0)

    payload = gzip.decompress(stored)
    records, extension = convergence.decode_extended_page(payload)

    assert [item["id"] for item in records] == [page[0]["id"]]
    assert records[0]["address_levels"] == page[0]["address_levels"]
    assert records[0]["street"] == page[0]["street"]
    assert set(extension[0]["division_gers_ids"]) == set(page[0]["division_gers_ids"])
    assert extension[0]["match_method"] == convergence.MATCH_METHOD_INTERIOR
    assert extension[0]["match_confidence"] == 2


def test_extended_page_rejects_truncation_and_trailing_bytes():
    payload = convergence.encode_extended_page(extended_page_fixture())
    with pytest.raises(ValueError, match="trailing extended-page bytes"):
        convergence.decode_extended_page(payload + b"\x00")
    with pytest.raises(ValueError):
        convergence.decode_extended_page(payload[:-1])
    with pytest.raises(ValueError, match="truncated extended-page core"):
        convergence.decode_extended_page(convergence.encode_uvarint(10_000))


def test_division_extension_rejects_out_of_range_index():
    payload = (
        convergence.encode_uvarint(1)
        + uuid.UUID(int=1).bytes
        + convergence.encode_uvarint(1)
        + convergence.encode_uvarint(5)  # dictionary only has index 0
        + bytes([convergence.encode_match_byte(1, 0)])
    )
    with pytest.raises(ValueError, match="out of range"):
        convergence.decode_division_extension(payload, 1)


# ----------------------------------------------------------------------------
# Storage measurement + paging (uses the imported compression encoder).
# ----------------------------------------------------------------------------
def storage_record(index: int, *, number: str, ids):
    feature_id = str(uuid.UUID(int=index))
    record = {
        "id": feature_id,
        "lon": -71.1,
        "lat": 42.37,
        "country": "US",
        "postal_city": "Cambridge",
        "postcode": "02139",
        "street": "Main Street",
        "number": number,
        "unit": "",
        "address_levels": ["MA", "Cambridge"],
        "source_object_index": 0,
        "source_row_group": 0,
        "source_row_index": index,
        "division_gers_ids": [str(uuid.UUID(int=i)) for i in ids],
        "match_method": convergence.MATCH_METHOD_INTERIOR,
        "match_confidence": len(ids),
    }
    record["key"] = convergence.reduce.record_key(record)
    return record


def test_paginate_never_splits_a_candidate_group():
    records = [
        storage_record(1, number="10", ids=[1]),
        storage_record(2, number="10", ids=[1]),
        storage_record(3, number="10", ids=[1]),
        storage_record(4, number="11", ids=[1]),
    ]
    ordered = sorted(records, key=lambda item: item["key"])
    pages = list(convergence.paginate(ordered, page_rows=1))
    # page_rows=1 but the three identical-key "10" rows must share a page.
    first_keys = [page[0]["key"][:8] for page in pages]
    assert len(first_keys) == len(set(first_keys))
    reassembled = [record for page in pages for record in page]
    assert reassembled == ordered


def test_measure_storage_reports_positive_division_delta():
    records = [
        storage_record(index, number=str(index), ids=[index, index + 1])
        for index in range(1, 9)
    ]
    result = convergence.measure_storage(records, page_rows=4)
    assert result["rows"] == 8
    assert result["extended_bytes_per_row"] > result["baseline_bytes_per_row"]
    assert result["delta_bytes_per_row"] > 0
    assert (
        result["linear_extended_all_planning_rows_gb"]
        >= (result["linear_baseline_all_planning_rows_gb"])
    )


def test_confidence_bucket_is_clamped():
    assert convergence._confidence_bucket(0) == 0
    assert convergence._confidence_bucket(3) == 3
    assert convergence._confidence_bucket(99) == 15


def test_sample_mismatches_dedupes_by_label_shape_and_counts():
    mismatch = row(
        al_locality="Charlestown",
        postal_city="",
        cont_locality="Boston",
        match_method=convergence.MATCH_METHOD_INTERIOR,
    )
    other = row(
        al_locality="Brighton",
        postal_city="",
        cont_locality="Boston",
        match_method=convergence.MATCH_METHOD_INTERIOR,
    )
    agree = row()  # exact/normalization agreement; must not appear in examples
    examples = convergence.sample_mismatches([mismatch, mismatch, other, agree])
    assert len(examples) == 2
    assert examples[0]["address_levels_locality"] == "Charlestown"
    assert examples[0]["occurrences"] == 2
    assert examples[1]["address_levels_locality"] == "Brighton"
    assert examples[1]["occurrences"] == 1
    assert all(item["category"] == "unresolved_disagreement" for item in examples)
