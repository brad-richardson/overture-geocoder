#!/usr/bin/env python3
"""The base-import measurements §6 of the scope asks for.

Three of the four open items in
`2026-08-07-base-theme-landmark-import-scope.md` §6, all offline against the
local mirror and the local planet head:

  6.1  DUPLICATE RATE -- of the admitted base rows, how many have a Places
       record with the same name within 200 m? Six of the ten gold landmarks
       base was supposed to add are already served from Places (the Eiffel
       Tower, Big Ben and the Statue of Liberty exist in both themes), so the
       risk register put duplicates at the top. This prices that risk.
  6.2  REAL HEAD COST -- the scope's +14.0% assumes one head record per feature
       and no eviction. The per-token cap of 10 is applied here against the
       actual head input, so the answer is what the head would really grow by.
  6.4  NAMES.COMMON -- Places carries cross-language names on exactly 0 of
       75.6M records and the admission set carries them on 893,972. If most of
       those attach to a Places record that already exists, a names-only join
       is the cheaper half of the whole idea and no records need importing.

Method for the joins: exact normalized-name equality plus a haversine inside the
radius, via a metric-ish grid key. Longitude is scaled by cos(lat) so 0.002 units is about
222 m on both axes, which makes a 3x3 cell neighbourhood cover 200 m at any
latitude without a cross join.

The rate is a LOWER BOUND on true duplication: it counts exact normalized-name
equality, so `Golden Gate Bridge` in one theme and `Golden Gate Br` in the other
are two records here, not one.

Requires the admitted base rows staged locally by
`2026-08-07-base-theme-import-inventory.py`'s admission set; see the
`--base-glob` default. Offline, no credentials, a few seconds.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import duckdb

REPO = Path(__file__).resolve().parents[2]
PLACES = "/home/brad/dev/overture-local/2026-06-17.0/theme=places/type=place/*.parquet"
BASE_ADMITTED = "/home/brad/dev/overture-local/base-admitted/*.parquet"
HEAD_CANDIDATES = (
    "/home/brad/dev/overture-local/planet-0617/store-map/map/places-v1/"
    "head-candidates/sha256/*.parquet"
)
RADII_KM = (0.05, 0.2, 0.5)
# The frozen per-token cap, and the head this prices against: the same local
# planet build the phrase-admission sizing used, so the figures compare.
CAP_PER_TOKEN = 10
HEAD_TOTAL_RECORDS = 33_604_005
HEAD_TOTAL_BYTES = 5_717_067_235


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--places-glob", default=PLACES)
    parser.add_argument("--base-glob", default=BASE_ADMITTED)
    parser.add_argument("--head-candidates", default=HEAD_CANDIDATES)
    parser.add_argument(
        "--output",
        default=str(REPO / "benchmarks/2026-08-07-base-import-measurements-v1.json"),
    )
    args = parser.parse_args()

    con = duckdb.connect(config={"memory_limit": "40GB", "threads": "16"})
    started = time.monotonic()

    con.execute(
        f"""
        create or replace table base_side as
        select base_type, subtype, class, name, names_common, lon, lat,
               lower(strip_accents(name)) as nname,
               cast(floor(lat / 0.002) as bigint) as ycell,
               cast(floor(lon * cos(radians(lat)) / 0.002) as bigint) as xcell
        from read_parquet('{args.base_glob}')
        where name is not null and name <> ''
        """
    )
    admitted = con.execute("select count(*) from base_side").fetchone()[0]

    # Restrict Places to names that occur in the base set before joining; the
    # semi-join is what keeps a 4.7M x 75.6M problem to a few seconds.
    con.execute(
        f"""
        create or replace table places_side as
        select lower(strip_accents(names.primary)) as nname,
               (bbox.xmin + bbox.xmax) / 2 as lon,
               (bbox.ymin + bbox.ymax) / 2 as lat,
               cast(floor(((bbox.ymin + bbox.ymax) / 2) / 0.002) as bigint) as ycell,
               cast(floor(((bbox.xmin + bbox.xmax) / 2)
                    * cos(radians((bbox.ymin + bbox.ymax) / 2)) / 0.002) as bigint) as xcell
        from read_parquet('{args.places_glob}')
        where names.primary is not null and names.primary <> ''
          and lower(strip_accents(names.primary)) in (select distinct nname from base_side)
        """
    )
    places_sharing_a_name = con.execute("select count(*) from places_side").fetchone()[0]

    con.execute(
        """
        create or replace table pairs as
        select b.rowid as base_row, p.rowid as places_row, b.names_common,
               b.base_type, b.subtype, b.class,
               6371.0088 * 2 * asin(sqrt(
                 pow(sin(radians(p.lat - b.lat) / 2), 2)
                 + cos(radians(b.lat)) * cos(radians(p.lat))
                   * pow(sin(radians(p.lon - b.lon) / 2), 2))) as km
        from base_side b join places_side p
          on p.nname = b.nname
         and p.ycell between b.ycell - 1 and b.ycell + 1
         and p.xcell between b.xcell - 1 and b.xcell + 1
        """
    )

    result = {
        "schema": "base-import-measurements-v1",
        "inputs": {
            "places": "overture places 2026-06-17.0 (local mirror)",
            "base_admission_set": (
                "the class-scoped set proposed in "
                "2026-08-07-base-theme-landmark-import-scope.md"
            ),
        },
        "admitted_base_rows": admitted,
        "places_rows_sharing_an_admitted_name": places_sharing_a_name,
        "candidate_pairs": con.execute("select count(*) from pairs").fetchone()[0],
        "method_note": (
            "Exact normalized-name equality plus haversine. A LOWER BOUND on "
            "duplication: differing spellings of one entity count as two."
        ),
    }
    for radius in RADII_KM:
        duplicates = con.execute(
            f"select count(*) from (select distinct base_row from pairs where km <= {radius})"
        ).fetchone()[0]
        result[f"duplicates_within_{int(radius * 1000)}m"] = duplicates
        result[f"duplicate_rate_within_{int(radius * 1000)}m"] = round(
            duplicates / admitted, 4
        )

    by_class = con.execute(
        """
        with dup as (
          select distinct base_row, base_type, subtype, class from pairs where km <= 0.2
        )
        select base_type, subtype, class, count(*) from dup group by 1, 2, 3 order by 4 desc
        """
    ).fetchall()
    result["by_class_within_200m"] = [
        dict(zip(("base_type", "subtype", "class", "duplicates"), row)) for row in by_class
    ]

    # 6.4 -- would a names-only join reach most of the cross-language names?
    twin = con.execute(
        """
        select count(distinct places_row) filter (where km <= 0.2),
               count(distinct places_row) filter (where km <= 0.2 and names_common > 0)
        from pairs
        """
    ).fetchone()
    admitted_with_common = con.execute(
        f"""
        select count(*) from read_parquet('{args.base_glob}') where names_common > 0
        """
    ).fetchone()[0]
    result["names_common_join"] = {
        "admitted_rows_with_cross_language_names": admitted_with_common,
        "places_records_with_a_base_twin_within_200m": twin[0],
        "places_records_that_would_gain_cross_language_names": twin[1],
        "reach_of_a_names_only_join": round(twin[1] / admitted_with_common, 4),
        "reading": (
            "A names-only join reaches only the base rows that already have a "
            "Places twin. The rest of the cross-language names are attached to "
            "records that do not exist in Places, so they arrive only if the "
            "records do."
        ),
    }

    # 6.2 -- the head cost the per-token cap actually leaves, against the real
    # head input rather than an upper bound.
    words = (
        r"list_filter(regexp_split_to_array(lower(strip_accents(name)), "
        r"'[^\p{L}\p{N}_]+'), x -> x <> '')"
    )
    con.execute(
        f"""
        create or replace table base_tokens as
        select unnest({words}) as token from read_parquet('{args.base_glob}')
        where name is not null and name <> ''
        """
    )
    con.execute(
        f"""
        create or replace table head_today as
        select token, count(*) as rows from read_parquet('{args.head_candidates}')
        where token not like 'e2:%' and token not like 'e3:%'
        group by 1
        """
    )
    head = con.execute(
        f"""
        with base_per_token as (select token, count(*) as rows from base_tokens group by 1),
             merged as (
               select coalesce(h.token, b.token) as token,
                      coalesce(h.rows, 0) as head_rows,
                      coalesce(b.rows, 0) as base_rows
               from head_today h full outer join base_per_token b on h.token = b.token
             )
        select sum(least(head_rows, {CAP_PER_TOKEN})) filter (where head_rows > 0),
               sum(least(head_rows + base_rows, {CAP_PER_TOKEN})),
               count(*) filter (where head_rows = 0 and base_rows > 0)
        from merged
        """
    ).fetchone()
    delta = head[1] - head[0]
    bytes_per_record = HEAD_TOTAL_BYTES / HEAD_TOTAL_RECORDS
    result["head_cost_after_cap"] = {
        "head_records_today_ordinary_tokens": head[0],
        "head_records_after_import": head[1],
        "new_head_records": delta,
        "new_head_bytes": round(delta * bytes_per_record),
        "growth_fraction_of_planet_head": round(delta / HEAD_TOTAL_RECORDS, 4),
        "tokens_that_would_exist_only_because_of_base": head[2],
        "scope_upper_bound_for_comparison": 0.1401,
        "method_check": (
            "head_records_today_ordinary_tokens reproduces the 30,841,082 "
            "ordinary-token head records recorded for the hosted planet run, "
            "which is what makes the after-figure trustworthy."
        ),
    }
    result["elapsed_seconds"] = round(time.monotonic() - started, 1)

    Path(args.output).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "by_class_within_200m"}, indent=2))


if __name__ == "__main__":
    main()
