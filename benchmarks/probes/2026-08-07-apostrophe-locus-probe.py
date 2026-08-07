#!/usr/bin/env python3
"""Where does apostrophe folding have to live -- the Worker or the producer?

`docs/plans/2026-08-04-v5-build-readiness.md` §3.4 defers the tokenizer/alias
work with an explicit instruction: "verify whether folding is Worker-side (query
normalization) or producer-side (index keys) before scheduling, since that
determines whether it needs v5 at all." That question was never answered, and
the 2026-08-06 variant stratum then measured the class at 0/5 for BOTH
spellings, which is worse than either lane predicts on its own.

This probe answers it with two measurements, and deliberately keeps them apart:

CORPUS (offline, local mirror of the release production serves)
  How the index tokenizer -- `normalized_words` in
  `crates/geocoder-construction/src/bin/places_transform_v1.rs`, mirrored
  byte-for-byte by `crates/geocoder-worker/src/places_pages.rs` -- treats an
  apostrophe: it is a separator, so `Len's Mill Store` indexes as
  [len, s, mill, store]. That inflates the token count, and
  `HEAD_QUERY_TOKEN_CAP = 3` refuses a query before any read.

LIVE (production, a handful of paired requests)
  The same name typed both ways, to show which spelling each lane can serve.

The two halves are separable and this probe measures the split, because it
decides what can ship without a rebuild:

  * an apostrophe-TYPED query is a Worker problem -- the tokens exist in the
    index, the query just cannot get under the cap;
  * an ASCII-TYPED query is a producer problem -- the token `dominos` is not in
    the index at all, and no query-time rule can synthesize it without also
    firing on ordinary plurals.

Requires the local planet staging in `docs/local-staging.md` for the corpus half
and network for the live half; `--skip-live` runs the corpus half alone.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import duckdb
import requests

REPO = Path(__file__).resolve().parents[2]
SOURCE = "/home/brad/dev/overture-local/2026-06-17.0/theme=places/type=place/*.parquet"

# Derived from the same local build the phrase-admission sizing used, so the
# byte figures in the two documents are comparable.
HEAD_TOTAL_RECORDS = 33_604_005
HEAD_TOTAL_BYTES = 5_717_067_235
CAP_PER_TOKEN = 10

LOW = "lower(strip_accents(names.primary))"
SPLIT = (
    rf"list_filter(regexp_split_to_array({LOW}, '[^\p{{L}}\p{{N}}_]+'), x -> x <> '')"
)
JOINED = (
    rf"list_filter(regexp_split_to_array("
    rf"regexp_replace({LOW}, '[''’]', '', 'g'), '[^\p{{L}}\p{{N}}_]+'), x -> x <> '')"
)

# Paired spellings. Each pair isolates ONE variable: same target, same lane,
# only the apostrophe differs.
LIVE_PAIRS = [
    ("Domino's Pizza", "Dominos Pizza"),
    ("Queen's Medical Center", "Queens Medical Center"),
    ("Women's Health Center", "Womens Health Center"),
    ("Shriners Children's Hawaii", "Shriners Childrens Hawaii"),
    ("Len's Mill Store", "Lens Mill Store"),
]


def corpus_measurements(con: duckdb.DuckDBPyConnection) -> dict:
    shape = con.execute(
        f"""
        with base as (
          select names.primary as name, {SPLIT} as split_words, {JOINED} as joined_words
          from read_parquet('{SOURCE}')
          where names.primary is not null and names.primary <> ''
        )
        select
          count(*) filter (where regexp_matches(name, '[''’]')) as apostrophe_records,
          count(*) filter (
            where regexp_matches(name, '[''’]')
              and len(list_filter(split_words, w -> length(w) <= 1)) > 0
          ) as apostrophe_with_degenerate_token,
          count(*) filter (
            where regexp_matches(name, '[''’]') and list_contains(split_words, 's')
          ) as emits_a_bare_s,
          count(*) filter (
            where regexp_matches(name, '[''’]') and len(split_words) > 3
          ) as over_head_cap_today,
          count(*) filter (
            where regexp_matches(name, '[''’]') and len(joined_words) > 3
          ) as over_head_cap_if_joined,
          count(*) filter (
            where regexp_matches(name, '[''’]')
              and len(split_words) > 3 and len(joined_words) <= 3
          ) as rescued_by_joining,
          count(*) filter (
            where regexp_matches(name, '[''’]') and len(split_words) between 2 and 3
          ) as phrase_eligible_today,
          count(*) filter (
            where regexp_matches(name, '[''’]') and len(joined_words) between 2 and 3
          ) as phrase_eligible_if_joined,
          count(*) filter (
            where len(list_filter(split_words, w -> length(w) <= 1)) > 0
              and not regexp_matches(name, '[''’]')
          ) as degenerate_without_any_apostrophe
        from base
        """
    ).fetchone()
    keys = [
        "apostrophe_records",
        "apostrophe_with_degenerate_token",
        "emits_a_bare_s",
        "over_head_cap_today",
        "over_head_cap_if_joined",
        "rescued_by_joining",
        "phrase_eligible_today",
        "phrase_eligible_if_joined",
        "degenerate_without_any_apostrophe",
    ]
    return dict(zip(keys, shape))


def producer_cost(con: duckdb.DuckDBPyConnection) -> dict:
    """What indexing the joined form would add to the head."""
    con.execute(
        f"""
        create or replace table apostrophe_new_tokens as
        with base as (
          select {SPLIT} as split_words, {JOINED} as joined_words
          from read_parquet('{SOURCE}')
          where names.primary is not null and regexp_matches(names.primary, '[''’]')
        )
        select unnest(
          list_filter(joined_words, w -> not list_contains(split_words, w))
        ) as token
        from base
        """
    )
    row = con.execute(
        f"""
        with per_token as (
          select token, count(*) as rows from apostrophe_new_tokens group by 1
        )
        select count(*), sum(rows), sum(least(rows, {CAP_PER_TOKEN})) from per_token
        """
    ).fetchone()
    bytes_per_record = HEAD_TOTAL_BYTES / HEAD_TOTAL_RECORDS
    return {
        "distinct_new_index_tokens": row[0],
        "new_record_token_rows": row[1],
        "new_head_records": row[2],
        "new_head_bytes": round(row[2] * bytes_per_record),
        "head_growth_fraction": row[2] / HEAD_TOTAL_RECORDS,
    }


def live_pairs(base_url: str, interval: float) -> list[dict]:
    rows = []
    for apostrophe, ascii_typed in LIVE_PAIRS:
        pair = {"apostrophe_query": apostrophe, "ascii_query": ascii_typed}
        for key, query in (("apostrophe", apostrophe), ("ascii", ascii_typed)):
            response = requests.get(
                f"{base_url.rstrip('/')}/v2/forward",
                params={"q": query, "types": "poi", "limit": "3"},
                timeout=30,
            )
            features = response.json().get("features", [])
            pair[key] = {
                "status": response.status_code,
                "empty": not features,
                "names": [
                    (feature.get("properties") or {}).get("name")
                    for feature in features[:3]
                ],
            }
            time.sleep(interval)
        rows.append(pair)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output", default=str(REPO / "benchmarks/2026-08-07-apostrophe-locus-v1.json")
    )
    parser.add_argument("--base-url", default="https://geocoder.bradr.dev")
    parser.add_argument("--interval", type=float, default=0.4)
    parser.add_argument("--skip-live", action="store_true")
    args = parser.parse_args()

    con = duckdb.connect(config={"memory_limit": "40GB", "threads": "16"})
    result = {
        "schema": "apostrophe-locus-v1",
        "question": (
            "v5-readiness §3.4: is apostrophe folding Worker-side or "
            "producer-side? Answer: both, and the halves are separable."
        ),
        "inputs": {
            "corpus": "overture places 2026-06-17.0 (local mirror)",
            "head_reference_build": "local planet build of 2026-06-17.0",
            "live_endpoint": None if args.skip_live else args.base_url,
        },
        "corpus": corpus_measurements(con),
        "producer_side_cost": producer_cost(con),
        "mechanism": {
            "index_tokenizer": "places_transform_v1.rs normalized_words",
            "query_tokenizer": "places_pages.rs normalized_words (byte-identical)",
            "head_query_token_cap": 3,
            "note": (
                "The apostrophe is a separator on both sides, so the two "
                "spellings of one name produce disjoint token sets, and the "
                "possessive costs a token-cap slot. A bare `s` posting is "
                "already survivable -- the saturated-posting relaxation in "
                "places_construction_v1.rs admits a record absent from a "
                "saturated posting when its display tokens carry the token -- "
                "so the cap, not the `s`, is what refuses these queries."
            ),
        },
    }
    if not args.skip_live:
        result["live_pairs"] = live_pairs(args.base_url, args.interval)

    Path(args.output).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "live_pairs"}, indent=2))


if __name__ == "__main__":
    main()
