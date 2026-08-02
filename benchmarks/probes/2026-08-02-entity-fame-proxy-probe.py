#!/usr/bin/env python3
"""Does any ENTITY-level signal separate a famous landmark from the obscure
same-category records that currently outrank it in the head list?

CONTEXT. Measured 2026-08-02 against live build `2026-08-02.0`: a multi-token
places query returns results iff the target is in the top-10 head list of EVERY
token. `q=Eiffel Tower` returns zero because the Eiffel Tower is absent from
`tower`'s top 10 -- and all ten entries that beat it are category `monument`,
exactly like the Eiffel Tower. The build-time key is

    ((field_mask & 3) != 0) DESC, prominence_rank DESC, confidence_rank DESC,
    feature_id, ...

`prominence_rank` is a 28-entry CATEGORY prior with 11 distinct values
(`scripts/places_type_prior_v1.py`), so every `monument` ties at 255.
`confidence_rank` is a per-source flat constant. The effective discriminator is
therefore `feature_id` -- UUID order -- and the cap of 10 picks an arbitrary ten
monuments from a global pool of thousands. That is why Wave C's reorder at cap
10 could not move the head path, and why a modest cap raise cannot reliably fix
it.

HYPOTHESIS UNDER TEST. Overture already carries entity-level fame proxies that
would separate them:

    H1  record multiplicity  -- a famous entity is conflated into many records
    H2  translation count    -- CARDINALITY(names.common), how many languages
    H3  source multiplicity  -- LEN(sources), how many upstreams carry it

If any of these cleanly orders the famous entities above the head-list
incumbents, it is a candidate build-time prominence signal. If none does, none
of these three raw proxies is sufficient as the direct build ordering for this
cohort.

SCOPE, deliberately bounded: one token (`tower`), the four famous entities a
user would expect, and the exact ten records measured in the live head list.
Every query is bbox-pruned; nothing scans the planet. Reads Overture from S3,
needs no credentials, writes only its evidence JSON when --output is given.

Run:  uv run --with 'duckdb>=1.5' python3 <this file>
"""
from __future__ import annotations

import argparse
import json
import statistics

import duckdb

REL = "2026-06-17.0"
SRC = f"s3://overturemaps-us-west-2/release/{REL}/theme=places/type=place/*"

# RE2-safe normaliser. `[^a-z]` collapses 69% of Tokyo records to the empty
# string; `\p{L}\p{N}` does not. See the 2026-08-01 probe's LANDMINE note.
NORM = r"regexp_replace(lower(strip_accents(names.primary)), '[^\p{L}\p{N}]', '', 'g')"

# (label, cohort, lon, lat, name_substring)
#
# `famous`   -- what a user typing "<name>" expects to get back.
# `head_list`-- the ten records MEASURED in `tower`'s live top-10 on
#               2026-08-02, in the order the API returned them. These are the
#               records that actually beat the Eiffel Tower.
ENTITIES = [
    ("Eiffel Tower",           "famous",    2.29448, 48.85837, "eiffel"),
    ("Tokyo Tower",            "famous",  139.74540, 35.65861, "tokyo tower"),
    ("CN Tower",               "famous",  -79.38710, 43.64257, "cn tower"),
    # "pisa" alone matches every Pisan business in the bbox; the entity is
    # named for its lean, not its city.
    ("Leaning Tower of Pisa",  "famous",   10.39664, 43.72300, "pendente"),

    ("Tower Grove House",      "head_list", -90.25400, 38.60500, "tower grove"),
    ("Alloa Tower",            "head_list",  -3.79360, 56.11470, "alloa"),
    # "bel" matches Isabel/Belo/Belas across Lisbon; use the placename.
    ("Torre de Belem",         "head_list",  -9.21600, 38.69160, "belem"),
    ("The Round Tower",        "head_list",  -1.10600, 50.78900, "round tower"),
    ("Birkenhead Priory",      "head_list",  -3.01300, 53.39300, "birkenhead"),
    ("The Clock Tower",        "head_list",  -0.13400, 51.50100, "clock tower"),
    ("Cabot Tower",            "head_list",  -2.62160, 51.45450, "cabot"),
    ("Bancroft Tower",         "head_list", -71.79500, 42.28000, "bancroft"),
    ("Jaywick Martello Tower", "head_list",   1.13000, 51.77500, "martello"),
    ("Lendal Tower",           "head_list",  -1.08700, 53.96000, "lendal"),
]

# Generous enough to survive a coordinate guess, tight enough to prune hard.
HALF_DEG = 0.08


def measure(con: duckdb.DuckDBPyConnection, lon: float, lat: float,
            needle: str) -> dict:
    """Entity-level proxies for every record matching `needle` in the bbox."""
    rows = con.execute(
        f"""
        SELECT
            {NORM}                                        AS norm,
            names.primary                                 AS primary_name,
            CARDINALITY(names.common)                     AS translations,
            LEN(sources)                                  AS n_sources,
            categories.primary                            AS category
        FROM read_parquet('{SRC}', filename=true, hive_partitioning=1)
        WHERE bbox.xmin BETWEEN ? AND ?
          AND bbox.ymin BETWEEN ? AND ?
          AND names.primary IS NOT NULL
          AND lower(strip_accents(names.primary)) LIKE ?
        """,
        [lon - HALF_DEG, lon + HALF_DEG,
         lat - HALF_DEG, lat + HALF_DEG,
         f"%{needle}%"],
    ).fetchall()

    if not rows:
        return {"located": False, "records": 0}

    translations = [r[2] or 0 for r in rows]
    sources = [r[3] or 0 for r in rows]
    return {
        "located": True,
        "records": len(rows),                       # H1
        "name_forms": len({r[0] for r in rows}),
        "translations_max": max(translations),      # H2
        "translations_median": statistics.median(translations),
        "sources_max": max(sources),                # H3
        "sources_median": statistics.median(sources),
        "categories": sorted({r[4] for r in rows if r[4]})[:4],
        "example": rows[0][1],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", help="write evidence JSON here")
    args = ap.parse_args()

    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2';")

    results = {}
    print(f"{'entity':24} {'cohort':10} {'recs':>5} {'forms':>6} "
          f"{'tr_max':>7} {'tr_med':>7} {'src_max':>8}")
    print("-" * 76)
    for label, cohort, lon, lat, needle in ENTITIES:
        m = measure(con, lon, lat, needle)
        m["cohort"] = cohort
        results[label] = m
        if not m["located"]:
            print(f"{label:24} {cohort:10} {'--- not located in bbox ---':>40}")
            continue
        print(f"{label:24} {cohort:10} {m['records']:>5} {m['name_forms']:>6} "
              f"{m['translations_max']:>7} {m['translations_median']:>7.1f} "
              f"{m['sources_max']:>8}")

    # The decisive comparison: does any proxy put EVERY famous entity above
    # EVERY head-list incumbent? A single overlap refutes clean separation.
    verdict = {}
    for proxy in ("records", "translations_max", "sources_max"):
        fam = [v[proxy] for v in results.values()
               if v["cohort"] == "famous" and v["located"]]
        obs = [v[proxy] for v in results.values()
               if v["cohort"] == "head_list" and v["located"]]
        if not fam or not obs:
            continue
        verdict[proxy] = {
            "famous_min": min(fam), "famous_median": statistics.median(fam),
            "head_list_max": max(obs),
            "head_list_median": statistics.median(obs),
            "separates_cleanly": min(fam) > max(obs),
        }

    print("\n=== separation ===")
    for proxy, v in verdict.items():
        mark = "CLEAN" if v["separates_cleanly"] else "OVERLAP"
        print(f"  {proxy:18} famous_min={v['famous_min']:>5} "
              f"head_list_max={v['head_list_max']:>5}   -> {mark}")

    if args.output:
        with open(args.output, "w") as handle:
            json.dump({"schema": "entity-fame-proxy-probe-v1",
                       "overture_release": REL,
                       "token": "tower",
                       "entities": results,
                       "verdict": verdict}, handle, indent=2, sort_keys=True)
        print(f"\nwrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
