"""Contract tests for the POI category prominence prior.

Every assertion here is pinned to a MEASURED record from Overture places
`2026-06-17.0`. The probe that produced them is
`benchmarks/probes/2026-07-31-poi-type-prior-probe.py`; the write-up is Part 6d
of `docs/plans/2026-07-31-search-quality-and-street-layer.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import places_type_prior_v1 as prior  # noqa: E402


# Real records, copied verbatim from the probe output.
BASILICA = dict(
    primary="catholic_church",
    basic="christian_place_of_worship",
    taxonomy="roman_catholic_place_of_worship",
    alternate=["landmark_and_historical_building", "monument"],
)
STARBUCKS = dict(
    primary="coffee_shop",
    basic="coffee_shop",
    taxonomy="coffee_shop",
    alternate=["cafe", "restaurant"],
)
VET = dict(
    primary="veterinarian",
    basic="animal_or_pet_service",
    taxonomy="veterinarian",
    alternate=None,
)
HOTEL = dict(
    primary="hotel", basic="hotel", taxonomy="hotel", alternate=["accommodation"]
)
# A holiday rental that claims `monument` among its alternates. Without the
# commodity-primary rule this record outranks the basilica.
HOLIDAY_RENTAL = dict(
    primary="holiday_rental_home",
    basic="holiday_rental_home",
    taxonomy="holiday_rental_home",
    alternate=["monument", "landmark_and_historical_building"],
)
# Overture tags US apartment blocks as landmarks. Measured: "Marq West
# Seattle", "Neptune SLU Apartments Seattle".
APARTMENTS = dict(
    primary="landmark_and_historical_building",
    basic="landmark_and_historical_building",
    taxonomy=None,
    alternate=None,
)
SPACE_NEEDLE = dict(
    primary="monument", basic="monument", taxonomy="monument", alternate=None
)


def test_the_landmark_outranks_the_commodity_records_on_its_own_token():
    """The measured failure: all three share the token `sagrada`, and today
    confidence ranks them in exactly the wrong order (0.9897 / 0.9998 / 1.0)."""
    assert prior.type_prior(**BASILICA) > prior.type_prior(**STARBUCKS)
    assert prior.type_prior(**BASILICA) > prior.type_prior(**VET)
    assert prior.type_prior(**BASILICA) > prior.type_prior(**HOTEL)


def test_a_commodity_primary_is_dispositive_over_its_alternates():
    """`Sensation Sagrada Familia` is a holiday rental claiming `monument`.
    It must not outrank an actual basilica."""
    assert prior.type_prior(**HOLIDAY_RENTAL) == 0.0
    assert prior.type_prior(**HOLIDAY_RENTAL) < prior.type_prior(**BASILICA)


def test_noisy_landmark_category_stays_below_real_landmarks():
    """`landmark_and_historical_building` is applied to apartment blocks, so it
    must not reach the level of `monument`."""
    assert prior.type_prior(**APARTMENTS) < prior.type_prior(**SPACE_NEEDLE)
    assert prior.type_prior(**APARTMENTS) < prior.type_prior(**BASILICA)


def test_alternates_are_weaker_than_an_equivalent_primary():
    only_alt = prior.type_prior(primary=None, alternate=["monument"])
    as_primary = prior.type_prior(primary="monument")
    assert only_alt == prior.ALTERNATE_WEIGHT * as_primary
    assert only_alt < as_primary


def test_unknown_and_missing_categories_are_zero_not_an_error():
    assert prior.type_prior(None) == 0.0
    assert prior.type_prior("") == 0.0
    assert prior.type_prior("some_category_overture_added_last_week") == 0.0
    assert prior.type_prior(None, None, None, None) == 0.0
    assert prior.type_prior("shoe_store", alternate=[]) == 0.0


def test_prominence_rank_is_total_and_saturating():
    for record in (BASILICA, STARBUCKS, VET, HOTEL, HOLIDAY_RENTAL,
                   APARTMENTS, SPACE_NEEDLE):
        rank = prior.prominence_rank(**record)
        assert isinstance(rank, int)
        assert 0 <= rank <= prior.PROMINENCE_RANK_MAX

    assert prior.prominence_rank(**SPACE_NEEDLE) == prior.PROMINENCE_RANK_MAX
    assert prior.prominence_rank(**STARBUCKS) == 0
    # Ordering survives quantization -- the whole point of the byte.
    assert prior.prominence_rank(**BASILICA) > prior.prominence_rank(**APARTMENTS)


def test_every_commodity_category_scores_zero_as_a_primary():
    for category in prior.COMMODITY_CATEGORIES:
        assert prior.type_prior(category) == 0.0, category


def test_landmark_table_values_are_in_range():
    for category, value in prior.LANDMARK_PRIOR.items():
        assert 0.0 < value <= 1.0, category
    # A category must not be in both tables -- that would make the result
    # depend on which check ran first.
    assert not (set(prior.LANDMARK_PRIOR) & prior.COMMODITY_CATEGORIES)
