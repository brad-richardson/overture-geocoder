#!/usr/bin/env python3
"""Audit Places prominence/commodity keys against planet source categories."""

from __future__ import annotations

import argparse
import difflib
import json
from pathlib import Path
import time

import duckdb


ROOT = Path(__file__).resolve().parents[2]
RELEASE = "2026-06-17.0"
SOURCE = (
    "s3://overturemaps-us-west-2/release/"
    f"{RELEASE}/theme=places/type=place/*"
)
FOCUS = {
    "atm",
    "atms",
    "stadium",
    "stadium_arena",
    "opera_and_ballet",
    "hospital",
}
# Freeze the pre-correction contract this dated audit evaluated. Loading the
# live table would make the committed evidence non-reproducible after applying
# the corrections the audit itself recommends.
LANDMARK_PRIOR_AT_AUDIT = {
    "airport",
    "aquarium",
    "art_gallery",
    "art_museum",
    "castle",
    "cathedral",
    "catholic_church",
    "christian_place_of_worship",
    "historic_site",
    "history_museum",
    "landmark_and_historical_building",
    "library",
    "monument",
    "mosque",
    "museum",
    "palace",
    "park",
    "place_of_worship",
    "public_plaza",
    "seaplane_bases",
    "stadium_arena",
    "subway_station",
    "synagogue",
    "temple",
    "theatre",
    "tourist_attraction",
    "train_station",
    "university",
    "zoo",
}
COMMODITY_CATEGORIES_AT_AUDIT = {
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


def run(output: Path, memory_limit: str, threads: int) -> dict:
    connection = duckdb.connect()
    connection.execute("INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2'")
    connection.execute(f"SET memory_limit='{memory_limit}'")
    connection.execute(f"SET threads={threads}")
    connection.execute("SET preserve_insertion_order=false")
    started = time.monotonic()
    rows = []
    field_queries = {
        "primary": f"""
            SELECT categories.primary AS category, count(*)::BIGINT AS records
            FROM read_parquet('{SOURCE}', hive_partitioning=true)
            WHERE categories.primary IS NOT NULL
              AND trim(categories.primary) != ''
            GROUP BY categories.primary
        """,
        "basic": f"""
            SELECT basic_category AS category, count(*)::BIGINT AS records
            FROM read_parquet('{SOURCE}', hive_partitioning=true)
            WHERE basic_category IS NOT NULL AND trim(basic_category) != ''
            GROUP BY basic_category
        """,
        "alternate": f"""
            SELECT category, count(*)::BIGINT AS records
            FROM read_parquet('{SOURCE}', hive_partitioning=true),
                 UNNEST(COALESCE(categories.alternate, []::VARCHAR[])) alternates(category)
            WHERE category IS NOT NULL AND trim(category) != ''
            GROUP BY category
        """,
    }
    for field, query in field_queries.items():
        field_rows = connection.execute(query).fetchall()
        rows.extend((field, category, records) for category, records in field_rows)
        print(
            json.dumps(
                {"completed_field": field, "distinct_categories": len(field_rows)},
                sort_keys=True,
            ),
            flush=True,
        )
    elapsed = time.monotonic() - started
    by_category: dict[str, dict[str, int]] = {}
    for field, category, records in rows:
        by_category.setdefault(category, {})[field] = int(records)
    observed = sorted(by_category)

    configured = {
        "landmark_prior": sorted(LANDMARK_PRIOR_AT_AUDIT),
        "commodity": sorted(COMMODITY_CATEGORIES_AT_AUDIT),
    }
    contracts = {}
    for contract, keys in configured.items():
        contracts[contract] = {}
        for key in keys:
            fields = by_category.get(key, {})
            contracts[contract][key] = {
                "fields": fields,
                "records": sum(fields.values()),
                "closest_observed": (
                    []
                    if fields
                    else difflib.get_close_matches(key, observed, n=5, cutoff=0.72)
                ),
            }

    evidence = {
        "schema": "overture-places-category-contract-audit-v1",
        "source_release": RELEASE,
        "contract_snapshot": "before-2026-08-03-category-corrections",
        "parameters": {
            "duckdb_version": duckdb.__version__,
            "duckdb_memory_limit": memory_limit,
            "duckdb_threads": threads,
            "preserve_insertion_order": False,
            "scan_strategy": "field-separated-v1",
        },
        "measurement": {
            "wall_seconds": elapsed,
            "distinct_categories": len(observed),
            "field_value_records": sum(sum(fields.values()) for fields in by_category.values()),
        },
        "configured_contracts": contracts,
        "focus": {
            key: {
                "fields": by_category.get(key, {}),
                "records": sum(by_category.get(key, {}).values()),
            }
            for key in sorted(FOCUS)
        },
        "observed_categories": by_category,
    }
    output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--memory-limit", default="8GB")
    parser.add_argument("--threads", type=int, default=2)
    args = parser.parse_args()
    result = run(args.output, args.memory_limit, args.threads)
    print(
        json.dumps(
            {
                "focus": result["focus"],
                "missing_landmark_keys": [
                    key
                    for key, item in result["configured_contracts"][
                        "landmark_prior"
                    ].items()
                    if item["records"] == 0
                ],
                "missing_commodity_keys": [
                    key
                    for key, item in result["configured_contracts"]["commodity"].items()
                    if item["records"] == 0
                ],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
