#!/usr/bin/env python3
"""Size a theme=base landmark import, and separate it from the named-after trap.

`2026-08-06-places-failure-mode-review.md` §6 calls the theme=base landmark
import "the only living fame lever", citing 14 gold rows "all currently
unservable". This probe measures what an import would actually contain and what
it would actually add, because both halves of that citation turn out to need
correcting.

Two measurements:

INVENTORY (S3, the release production serves)
  Named rows in the three scanned base types, grouped by (subtype, class), with
  wikidata-QID and names.common coverage. Places carries `names.common` on
  exactly 0 of 75.6M records, so this is the only place in the corpus where
  cross-language names exist at all.

GOLD DELTA (offline, from the frozen 2026-08-05 coverage artifact)
  How many gold landmarks an import would ADD, separating three tiers that the
  headline "14 rows" conflates: rows vs distinct landmarks, transit stops named
  after a landmark vs the landmark, and landmarks production already serves.

The admission set is derived from where genuine (non-transit) gold landmarks
actually live, plus the natural-feature classes a geocoder is asked for
directly. It is a starting proposal, not a decision -- the point of the
per-class table is that the set can be argued about with numbers attached.

theme=buildings (276 GB) is NOT scanned, so every absent verdict means "not in
the three scanned base types", never "not in Overture".

Usage:
  .venv/bin/python benchmarks/probes/2026-08-07-base-theme-import-inventory.py
  (~8 minutes; reads ~282M rows over S3 with column pruning, no credentials)
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import duckdb

REPO = Path(__file__).resolve().parents[2]
RELEASE = "2026-06-17.0"
S3_BASE = f"s3://overturemaps-us-west-2/release/{RELEASE}/theme=base"
BASE_TYPES = ("infrastructure", "land_use", "land")
GOLD_COVERAGE = REPO / "benchmarks/2026-08-05-gold-coverage-in-base-theme.json"

# Head reference: the same local planet build the 2026-08-07 phrase-admission
# sizing priced against, so the byte figures in the two documents compare.
HEAD_TOTAL_RECORDS = 33_604_005
HEAD_TOTAL_BYTES = 5_717_067_235

# Derived from where the genuine (non-transit) gold landmarks live, plus the
# natural-feature classes a geocoder is asked for by name. `transit` is
# deliberately absent -- see the gold-delta half.
ADMISSION_SET = {
    "infrastructure": [
        ("tower", "bell_tower"),
        ("tower", "tower"),
        ("tower", "observation_tower"),
        ("bridge", "bridge"),
        ("pedestrian", "artwork"),
    ],
    "land_use": [
        ("protected", "national_park"),
        ("pedestrian", "plaza"),
        ("medical", "hospital"),
        ("medical", "clinic"),
        ("park", "park"),
        ("cemetery", "cemetery"),
        ("education", "university"),
        ("education", "college"),
    ],
    "land": [
        ("physical", "peak"),
        ("physical", "volcano"),
        ("physical", "saddle"),
        ("land", "island"),
        ("land", "islet"),
    ],
}


def inventory(con: duckdb.DuckDBPyConnection) -> dict:
    by_type: dict[str, list[dict]] = {}
    for base_type in BASE_TYPES:
        started = time.monotonic()
        rows = con.execute(
            f"""
            select subtype, class,
                   count(*) as named_rows,
                   count(*) filter (where wikidata is not null) as with_qid,
                   count(*) filter (
                     where coalesce(len(map_keys(names.common)), 0) > 0
                   ) as with_names_common
            from read_parquet('{S3_BASE}/type={base_type}/*.parquet')
            where names.primary is not null and names.primary <> ''
            group by 1, 2
            order by 3 desc
            """
        ).fetchall()
        by_type[base_type] = [
            dict(zip(("subtype", "class", "named_rows", "with_qid", "with_names_common"), row))
            for row in rows
        ]
        print(
            f"base/{base_type}: {sum(r[2] for r in rows):,} named rows "
            f"in {time.monotonic() - started:.0f}s",
            flush=True,
        )
    return by_type


def admitted(by_type: dict) -> dict:
    wanted = {
        base_type: {tuple(pair) for pair in pairs}
        for base_type, pairs in ADMISSION_SET.items()
    }
    picked, totals = [], {"named_rows": 0, "with_qid": 0, "with_names_common": 0}
    for base_type, entries in by_type.items():
        for entry in entries:
            if (entry["subtype"], entry["class"]) in wanted.get(base_type, set()):
                picked.append({"type": f"base/{base_type}", **entry})
                for key in totals:
                    totals[key] += entry[key]
    bytes_per_record = HEAD_TOTAL_BYTES / HEAD_TOTAL_RECORDS
    return {
        "classes": sorted(picked, key=lambda row: -row["named_rows"]),
        "totals": totals,
        "qid_coverage": round(totals["with_qid"] / totals["named_rows"], 4),
        "names_common_coverage": round(
            totals["with_names_common"] / totals["named_rows"], 4
        ),
        "upper_bound_head_records": totals["named_rows"],
        "upper_bound_head_bytes": round(totals["named_rows"] * bytes_per_record),
        "upper_bound_head_growth_fraction": totals["named_rows"] / HEAD_TOTAL_RECORDS,
        "cost_note": (
            "An upper bound that assumes one head record per admitted feature "
            "and no cap eviction. The per-token cap of 10 can only reduce it."
        ),
    }


def gold_delta() -> dict:
    coverage = json.loads(GOLD_COVERAGE.read_text())
    exact = [
        case
        for case in coverage["cases"]
        if (case.get("base") or {}).get("match_strength") == "EXACT"
    ]
    transit = [case for case in exact if case["base"]["subtype"] == "transit"]
    genuine = [case for case in exact if case["base"]["subtype"] != "transit"]

    landmarks: dict[str, dict] = {}
    for case in genuine:
        entry = landmarks.setdefault(
            case["expected_name"],
            {
                "class": f"{case['base']['subtype']}/{case['base']['class']}",
                "wikidata": case["base"].get("wikidata") is not None,
                "names_common": case["base"].get("names_common", 0),
                "production_serves_it_today": False,
                "rows": 0,
            },
        )
        entry["rows"] += 1
        if case["places"].get("production_hit"):
            entry["production_serves_it_today"] = True

    unserved = {
        name: entry
        for name, entry in landmarks.items()
        if not entry["production_serves_it_today"]
    }
    return {
        "exact_base_name_matches_rows": len(exact),
        "transit_named_after_rows": len(transit),
        "non_transit_rows": len(genuine),
        "distinct_landmarks_behind_those_rows": len(landmarks),
        "already_served_by_production": len(landmarks) - len(unserved),
        "incremental_gold_landmarks": len(unserved),
        "incremental": {name: entry for name, entry in sorted(unserved.items())},
        "landmarks": {name: entry for name, entry in sorted(landmarks.items())},
        "transit_examples": sorted(
            {
                f"{case['expected_name']} -> {case['base']['subtype']}/{case['base']['class']}"
                for case in transit
            }
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        default=str(REPO / "benchmarks/2026-08-07-base-theme-import-inventory-v1.json"),
    )
    parser.add_argument("--threads", type=int, default=16)
    parser.add_argument("--skip-inventory", action="store_true")
    args = parser.parse_args()

    result = {
        "schema": "base-theme-import-inventory-v1",
        "release": RELEASE,
        "scanned_types": [f"base/{one}" for one in BASE_TYPES],
        "not_scanned": [
            "theme=buildings (276 GB); an absent verdict is never 'not in Overture'",
            "base/water, base/land_cover: no gold case is a water or land-cover feature",
        ],
        "gold_delta": gold_delta(),
    }
    if not args.skip_inventory:
        con = duckdb.connect(config={"memory_limit": "32GB", "threads": str(args.threads)})
        con.execute("INSTALL httpfs; LOAD httpfs;")
        con.execute("SET s3_region='us-west-2';")
        by_type = inventory(con)
        result["inventory_by_class"] = by_type
        result["all_named_rows"] = sum(
            entry["named_rows"] for entries in by_type.values() for entry in entries
        )
        result["proposed_admission"] = admitted(by_type)

    Path(args.output).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result["gold_delta"], indent=2)[:1200])


if __name__ == "__main__":
    main()
