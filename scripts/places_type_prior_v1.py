#!/usr/bin/env python3
"""Static POI prominence prior derived from Overture place categories.

WHY THIS EXISTS, and why it is not built from any count column.

Measured 2026-07-31 against Overture places `2026-06-17.0` (evidence:
`benchmarks/probes/2026-07-31-poi-type-prior-probe.py`, written up as Part 6d
of `docs/plans/2026-07-31-search-quality-and-street-layer.md`):

  * `names.common` is empty for **100%** of places -- 0.0% non-empty over
    772,341 records across Barcelona, Paris, Seattle and Tokyo. There is no
    multilingual notability signal to mine.
  * `root_source_count` is 1 for every record. No corroboration signal.
  * `websites` / `socials` / `phones` are ~1/1/1 almost everywhere, and where
    they do vary they *anti*-correlate with prominence (SEO agencies win).
  * `confidence` anti-correlates outright. The Basilica de la Sagrada Familia
    scores 0.9897 while the Starbucks next door scores 0.9998 and a veterinary
    clinic scores 1.0000. Every count column on those three records is
    identical. Overture `confidence` is an EXISTENCE signal.

The only field that separates a landmark from a coffee shop is the category.
So this table is the prominence signal, and there is no second one.

CALIBRATION NOTES, each one measured rather than assumed:

  * `landmark_and_historical_building` is NOISY. Overture applies it to US
    apartment blocks -- "Marq West Seattle", "Neptune SLU Apartments Seattle",
    "Luna West Seattle" all carry it. It is therefore deliberately weak (0.35)
    where `monument` and `tourist_attraction` are strong.
  * The PRIMARY category is dispositive when it is a commodity type. A holiday
    rental that lists `monument` among its alternates is not a monument --
    without this rule "Sensation Sagrada Familia" (a holiday rental) outranks
    the basilica.
  * Alternates are a real but weaker signal, so they enter at half weight. The
    basilica reaches the cap through `catholic_church` (primary, 0.60), not
    through its `monument` alternate (0.50 after halving).

KNOWN LIMIT, do not mistake this table for a fix to it: a type prior cannot
separate near-duplicates of the SAME landmark. `Tour Eiffel` is still evicted
because 15 other `monument` records -- "Eiffel Tower,Paris", "Eiffel tower",
"Eiffel Tower,,Paris,Fra", "Toul Eiffel" -- describe the same tower with higher
confidence than the canonical record. That needs duplicate collapse and is
tracked separately.
"""

from __future__ import annotations

# Categories that carry genuine prominence, on a nominal 0-1 scale.
# Keys are matched against categories.primary, basic_category,
# taxonomy.primary, and categories.alternate.
LANDMARK_PRIOR: dict[str, float] = {
    # unambiguous landmarks
    "monument": 1.00,
    "tourist_attraction": 0.95,
    "airport": 0.90,
    # cultural institutions
    "museum": 0.85,
    "history_museum": 0.85,
    "art_museum": 0.85,
    "castle": 0.85,
    "palace": 0.85,
    "cathedral": 0.80,
    # transport hubs people actually search for by name
    "train_station": 0.55,
    "subway_station": 0.50,
    "seaplane_bases": 0.50,
    # worship: a named basilica is prominent, a parish church much less so
    "catholic_church": 0.60,
    "christian_place_of_worship": 0.55,
    "place_of_worship": 0.50,
    "synagogue": 0.55,
    "mosque": 0.55,
    "temple": 0.55,
    # civic and leisure
    "zoo": 0.60,
    "aquarium": 0.60,
    "university": 0.55,
    "art_gallery": 0.50,
    "stadium_arena": 0.45,
    "park": 0.45,
    "theatre": 0.45,
    "public_plaza": 0.45,
    "library": 0.40,
    # measured-noisy: Overture tags apartment blocks with this
    "landmark_and_historical_building": 0.35,
}

# Commodity types. A place whose PRIMARY category is one of these is not
# prominent regardless of what its alternates claim.
COMMODITY_CATEGORIES: frozenset[str] = frozenset(
    {
        "accommodation",
        "atm",
        "bank",
        "bar",
        "cafe",
        "coffee_shop",
        "convenience_store",
        "dentist",
        "fast_food_restaurant",
        "gas_station",
        "grocery_store",
        "gym",
        "hair_salon",
        "holiday_rental_home",
        "hotel",
        "insurance_agency",
        "laundry",
        "motel",
        "pharmacy",
        "real_estate_agent",
        "restaurant",
        "service_apartments",
        "veterinarian",
    }
)

# Weight applied to a match found only among categories.alternate.
ALTERNATE_WEIGHT = 0.5

# The serialized prior is a u8 so it can ride in the head entry beside
# confidence_rank. 255 == prior 1.0.
PROMINENCE_RANK_MAX = 255


def type_prior(
    primary: str | None,
    basic: str | None = None,
    taxonomy: str | None = None,
    alternate: object = None,
) -> float:
    """Prominence prior in [0, 1] from a place's category fields.

    `primary` being a commodity type is dispositive and returns 0.0 -- see the
    module docstring for why the alternates cannot be allowed to override it.
    """
    primary_tags = {value for value in (primary, basic, taxonomy) if value}
    if primary_tags & COMMODITY_CATEGORIES:
        return 0.0

    alternate_tags = {value for value in (alternate or []) if value}

    best_primary = max(
        (LANDMARK_PRIOR.get(tag, 0.0) for tag in primary_tags), default=0.0
    )
    best_alternate = max(
        (LANDMARK_PRIOR.get(tag, 0.0) for tag in alternate_tags), default=0.0
    )
    return max(best_primary, ALTERNATE_WEIGHT * best_alternate)


def prominence_rank(
    primary: str | None,
    basic: str | None = None,
    taxonomy: str | None = None,
    alternate: object = None,
) -> int:
    """`type_prior` quantized to the u8 the head entry carries.

    Saturating and total: every input maps into 0..=255.
    """
    prior = type_prior(primary, basic, taxonomy, alternate)
    if prior <= 0.0:
        return 0
    if prior >= 1.0:
        return PROMINENCE_RANK_MAX
    return min(PROMINENCE_RANK_MAX, round(prior * PROMINENCE_RANK_MAX))
