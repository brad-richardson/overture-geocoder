#!/usr/bin/env python3
"""Size the v5 phrase-admission decision: §3.1 `e4:` keys vs §3.2 softening.

`docs/plans/2026-08-04-v5-build-readiness.md` §3.1 and §3.2 both carry a
"Decision input PENDING" line and were never computed. This probe computes both,
offline, against the corpus production actually serves (Overture
`2026-06-17.0`), plus the head artifact of the complete local planet build of
that same release.

Two questions, one script:

COST -- what does each option add to the head?
  Replicates, in SQL, the producer's own prominence assignment
  (`scripts/places_type_prior_v1.py`, legacy `categories` path: primary tags =
  {categories.primary, basic_category}, alternates at half weight, a commodity
  primary dispositive) and `entity_phrase_key` /`normalized_words`
  (`crates/geocoder-construction/src/bin/places_transform_v1.rs`: 2..=3 words,
  `e{n}:` + words joined by one space). Applies the frozen per-token cap of 10.

YIELD -- which currently-missed cases does each option claim?
  For every case still missed at rank 10 by the deployed Worker `00bc46c` on
  both frozen sets, asks whether a record exists near the gold point whose
  primary name normalizes to the normalized query, whether that record is
  `prominence_rank == 0`, and how many words the name carries. A case is
  claimable by §3.2 only if all three line up; `4+` words is §3.1's
  constituency, and no near-gold name match is claimable by neither.

Yield is an UPPER BOUND: it measures that a phrase key would exist, not that the
result would rank. Ranking is a separate, unmeasured step.

Normalization caveat: DuckDB has no NFKD, so `strip_accents` stands in. Against
the `e2:`/`e3:` tokens the real build emitted into one planet head-candidate
shard this agreed on 21,706 of 21,765 keys (99.73%); every disagreement was a
compatibility form (styled math capitals, fullwidth Latin, `№`) and none changed
the word count, which is what the admission gate turns on. The planet-wide
cross-check is recorded in the output as `replication_crosscheck`.

Requires the local planet staging described in `docs/local-staging.md`; it makes
no network requests and needs no credentials.

Usage:
  .venv/bin/python benchmarks/probes/2026-08-07-phrase-admission-sizing-join.py \
      [--output benchmarks/2026-08-07-phrase-admission-sizing-v1.json]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import unicodedata
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import duckdb  # noqa: E402

import places_type_prior_v1 as type_prior  # noqa: E402

SOURCE = (
    "/home/brad/dev/overture-local/2026-06-17.0/theme=places/type=place/*.parquet"
)
HEAD_CANDIDATES = (
    "/home/brad/dev/overture-local/planet-0617/store-map/map/places-v1/"
    "head-candidates/sha256/*.parquet"
)

# From the completed local planet head of the same release:
# store-head/head/places/complete.json (records) and the 4,096 serve objects.
HEAD_TOTAL_RECORDS = 33_604_005
HEAD_TOTAL_BYTES = 5_141_583_720
HEAD_SHARDS = 4096
BYTES_PER_HEAD_RECORD = HEAD_TOTAL_BYTES / HEAD_TOTAL_RECORDS

# `acceptance_gates.head.result_cap_per_token`, frozen in the evidence spec.
CAP_PER_TOKEN = 10

RUNS = {
    "everyday": REPO / "benchmarks/2026-08-06-everyday-poi-post-worker-00bc46c.json",
    "gold": REPO / "benchmarks/2026-08-06-forward-gold-post-worker-00bc46c.json",
}
CASES = {
    "everyday": REPO / "benchmarks/everyday-poi-tripwire-cases-v1.json",
    "gold": REPO / "benchmarks/v2-forward-gold-cases-v1.json",
}
REBASELINE = REPO / "benchmarks/2026-08-06-everyday-denominator-rebaseline-v1.json"

WORDS_SQL = (
    r"list_filter(regexp_split_to_array(lower(strip_accents(names.primary)), "
    r"'[^\p{L}\p{N}_]+'), x -> x <> '')"
)


def sql_list(values) -> str:
    return "[" + ", ".join("'" + v.replace("'", "''") + "'" for v in sorted(values)) + "]"


def prominence_sql(extra_columns: str = "") -> str:
    """CTEs ending in `ranked`, carrying `type_prior` (0.0 <=> rank 0).

    The minimum non-zero prior is 0.175 (a `historic_site` alternate at half
    weight), which quantizes to rank 45, so `type_prior > 0` is exactly
    `prominence_rank > 0` and no rounding needs replicating.
    """
    commodity = sql_list(type_prior.COMMODITY_CATEGORIES)
    landmark = "\n".join(
        f"          when tag = '{name}' then {weight}"
        for name, weight in sorted(type_prior.LANDMARK_PRIOR.items())
    )
    passthrough = ("," + extra_columns) if extra_columns else ""
    carried = ""
    if extra_columns:
        carried = "," + ", ".join(
            part.split(" as ")[-1].strip() for part in extra_columns.split(",")
        )
    return f"""
    with base as (
      select
        names.primary as primary_name,
        list_filter([categories.primary, basic_category], x -> x is not null and x <> '')
          as primary_tags,
        coalesce(list_filter(categories.alternate, x -> x is not null and x <> ''), [])
          as alternate_tags,
        {WORDS_SQL} as words
        {passthrough}
      from read_parquet('{SOURCE}')
      where names.primary is not null and names.primary <> ''
    ),
    scored as (
      select
        primary_name,
        words,
        len(words) as word_count,
        len(list_filter(primary_tags, tag -> list_contains({commodity}, tag)))
          as commodity_hits,
        coalesce(list_max(list_transform(primary_tags, tag -> case
{landmark}
          else 0.0 end)), 0.0) as best_primary,
        coalesce(list_max(list_transform(alternate_tags, tag -> case
{landmark}
          else 0.0 end)), 0.0) as best_alternate
        {carried}
      from base
    ),
    ranked as (
      select
        primary_name,
        words,
        word_count,
        case when commodity_hits > 0 then 0.0
             else greatest(best_primary, 0.5 * best_alternate) end as type_prior
        {carried}
      from scored
    )
    """


def normalized_words(value: str) -> list[str]:
    """Port of `normalized_words` in places_transform_v1.rs (NFKD, exact)."""
    folded = "".join(
        character
        for character in unicodedata.normalize("NFKD", value.strip()).lower()
        if not unicodedata.combining(character)
    )
    words: list[str] = []
    current: list[str] = []
    for character in folded:
        if character.isalnum() or character == "_":
            current.append(character)
        elif current:
            words.append("".join(current))
            current = []
    if current:
        words.append("".join(current))
    return words


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0088
    first, second = math.radians(lat1), math.radians(lat2)
    delta_lat = second - first
    delta_lon = math.radians(lon2 - lon1)
    inner = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(first) * math.cos(second) * math.sin(delta_lon / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(inner))


def measure_cost(con: duckdb.DuckDBPyConnection) -> dict:
    con.execute(
        "create or replace table phrase as "
        + prominence_sql()
        + """
        select
          'e' || word_count || ':' || array_to_string(words, ' ') as phrase_key,
          word_count,
          (type_prior > 0) as prominent
        from ranked
        where word_count between 2 and 4
        """
    )

    shape = con.execute(
        prominence_sql()
        + """
        select
          (type_prior > 0) as prominent,
          case when word_count = 1 then '1'
               when word_count between 2 and 3 then '2-3'
               else '4+' end as words,
          count(*) as records
        from ranked
        group by 1, 2
        order by 1, 2
        """
    ).fetchall()

    emitted = con.execute(
        f"""
        select count(*) as rows, count(distinct token) as keys
        from read_parquet('{HEAD_CANDIDATES}')
        where token like 'e2:%' or token like 'e3:%'
        """
    ).fetchone()
    predicted = con.execute(
        """
        select count(*) as rows, count(distinct phrase_key) as keys
        from phrase where prominent and word_count between 2 and 3
        """
    ).fetchone()

    option_31 = con.execute(
        f"""
        with per_key as (
          select phrase_key, count(*) as rows
          from phrase where word_count = 4 and prominent
          group by 1
        )
        select count(*), sum(rows), sum(least(rows, {CAP_PER_TOKEN}))
        from per_key
        """
    ).fetchone()

    curve = []
    for limit in (1, 2, 3, 5, 10, None):
        predicate = "true" if limit is None else f"all_rows <= {limit}"
        row = con.execute(
            f"""
            with per_key as (
              select
                phrase_key,
                count(*) filter (where prominent) as prominent_rows,
                count(*) as all_rows
              from phrase
              where word_count between 2 and 3
              group by 1
            ),
            admitted as (
              select
                prominent_rows,
                case when {predicate} then all_rows else prominent_rows end as after_rows
              from per_key
            )
            select
              sum(least(prominent_rows, {CAP_PER_TOKEN})),
              sum(least(after_rows, {CAP_PER_TOKEN})),
              count(*) filter (where after_rows > prominent_rows)
            from admitted
            """
        ).fetchone()
        delta = row[1] - row[0]
        curve.append(
            {
                "admit_only_keys_shared_by_at_most": limit,
                "keys_gaining_records": row[2],
                "new_head_records": delta,
                "new_head_bytes": round(delta * BYTES_PER_HEAD_RECORD),
                "head_growth_fraction": delta / HEAD_TOTAL_RECORDS,
            }
        )

    full = curve[-1]
    return {
        "corpus_shape": [
            {"prominent": bool(a), "words": b, "records": c} for a, b, c in shape
        ],
        "replication_crosscheck": {
            "note": (
                "predicted from source vs emitted by the real planet build into "
                "head-candidates; keys are the admission unit"
            ),
            "predicted_phrase_rows": predicted[0],
            "emitted_phrase_rows": emitted[0],
            "predicted_phrase_keys": predicted[1],
            "emitted_phrase_keys": emitted[1],
            "key_agreement": 1 - abs(predicted[1] - emitted[1]) / emitted[1],
        },
        "option_3_1_e4_keys_prominent_only": {
            "new_keys": option_31[0],
            "candidate_rows": option_31[1],
            "new_head_records": option_31[2],
            "new_head_bytes": round(option_31[2] * BYTES_PER_HEAD_RECORD),
            "head_growth_fraction": option_31[2] / HEAD_TOTAL_RECORDS,
        },
        "option_3_2_admission_softening": {
            "new_keys": con.execute(
                """
                select count(distinct phrase_key) from phrase
                where not prominent and word_count between 2 and 3
                  and phrase_key not in (
                    select phrase_key from phrase
                    where prominent and word_count between 2 and 3
                  )
                """
            ).fetchone()[0],
            "new_head_records": full["new_head_records"],
            "new_head_bytes": full["new_head_bytes"],
            "head_growth_fraction": full["head_growth_fraction"],
        },
        "option_3_2_bounded_cost_curve": curve,
    }


def measure_yield(con: duckdb.DuckDBPyConnection) -> dict:
    quarantined = set(json.loads(REBASELINE.read_text())["quarantined_case_ids"])
    misses = []
    for scope, run_path in RUNS.items():
        run = json.loads(run_path.read_text())
        cases = {case["id"]: case for case in json.loads(CASES[scope].read_text())["cases"]}
        for row in run["results"]:
            if row.get("provider") != "overture" or row.get("found_at_10"):
                continue
            case = cases[row["case_id"]]
            misses.append(
                {
                    "scope": scope,
                    "case_id": row["case_id"],
                    "query": row["query"],
                    "lat": case["expected_lat"],
                    "lon": case["expected_lon"],
                    "tolerance_km": case.get("tolerance_km", 1.0),
                    "quarantined_absent": row["case_id"] in quarantined,
                    "words": normalized_words(row["query"]),
                }
            )

    wanted = sorted({" ".join(miss["words"]) for miss in misses if miss["words"]})
    con.execute("create or replace table wanted (normalized varchar)")
    con.executemany("insert into wanted values (?)", [(value,) for value in wanted])

    # Places are points, so the source bbox corners are the coordinate.
    matches = con.execute(
        prominence_sql(
            "(bbox.xmin + bbox.xmax) / 2 as longitude,"
            " (bbox.ymin + bbox.ymax) / 2 as latitude"
        )
        + """
        select
          array_to_string(words, ' ') as normalized,
          primary_name,
          word_count,
          (type_prior > 0) as prominent,
          longitude,
          latitude
        from ranked
        where array_to_string(words, ' ') in (select normalized from wanted)
        """
    ).fetchall()

    by_name: dict[str, list] = {}
    for row in matches:
        by_name.setdefault(row[0], []).append(row)

    detail = []
    for miss in misses:
        radius_km = max(1.0, 2.0 * miss["tolerance_km"])
        near = [
            row
            for row in by_name.get(" ".join(miss["words"]), [])
            if haversine_km(miss["lat"], miss["lon"], row[5], row[4]) <= radius_km
        ]
        word_count = len(miss["words"])
        prominent_near = sum(1 for row in near if row[3])
        if not near:
            verdict = "no_name_match_near_gold"
        elif word_count < 2:
            verdict = "phrase_ineligible_single_word"
        elif word_count > 3:
            # Only `e4:` on a prominent record can ever key a 4-word name.
            verdict = (
                "claimed_by_3_1"
                if word_count == 4 and prominent_near
                else "phrase_ineligible_long_name"
            )
        elif prominent_near < len(near):
            verdict = "claimed_by_3_2"
        else:
            verdict = "already_admitted_prominent"
        detail.append(
            {
                "scope": miss["scope"],
                "case_id": miss["case_id"],
                "query": miss["query"],
                "normalized_word_count": word_count,
                "near_gold_name_matches": len(near),
                "near_gold_prominent": prominent_near,
                "quarantined_absent": miss["quarantined_absent"],
                "search_radius_km": radius_km,
                "verdict": verdict,
            }
        )

    verdicts: dict[str, int] = {}
    by_scope: dict[str, dict[str, int]] = {}
    for row in detail:
        verdicts[row["verdict"]] = verdicts.get(row["verdict"], 0) + 1
        bucket = by_scope.setdefault(row["scope"], {})
        bucket[row["verdict"]] = bucket.get(row["verdict"], 0) + 1

    claimed_32 = [row for row in detail if row["verdict"] == "claimed_by_3_2"]
    return {
        "runs": {scope: str(path.relative_to(REPO)) for scope, path in RUNS.items()},
        "misses_at_rank_10": len(misses),
        "verdicts": verdicts,
        "verdicts_by_scope": by_scope,
        "claimed_by_3_2_but_quarantined_absent": [
            row["case_id"] for row in claimed_32 if row["quarantined_absent"]
        ],
        "upper_bound_note": (
            "A claim means the phrase key would exist and the record is refused "
            "today. It does not mean the case would rank; scoring is a separate "
            "step this probe does not measure."
        ),
        "cases": detail,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        default=str(REPO / "benchmarks/2026-08-07-phrase-admission-sizing-v1.json"),
    )
    parser.add_argument("--memory-limit", default="40GB")
    parser.add_argument("--threads", default="16")
    args = parser.parse_args()

    started = time.time()
    con = duckdb.connect(
        config={"memory_limit": args.memory_limit, "threads": args.threads}
    )
    result = {
        "schema": "phrase-admission-sizing-v1",
        "inputs": {
            "corpus": "overture places 2026-06-17.0 (local mirror)",
            "head_artifact": "local planet build of 2026-06-17.0",
            "worker_commit_under_test": "00bc46c",
            "data_build": "2026-08-03.0",
        },
        "head": {
            "total_records": HEAD_TOTAL_RECORDS,
            "total_bytes": HEAD_TOTAL_BYTES,
            "shards": HEAD_SHARDS,
            "bytes_per_record": BYTES_PER_HEAD_RECORD,
            "mean_shard_bytes": HEAD_TOTAL_BYTES / HEAD_SHARDS,
            "cap_per_token": CAP_PER_TOKEN,
        },
        "cost": measure_cost(con),
        "yield": measure_yield(con),
    }
    result["elapsed_seconds"] = round(time.time() - started, 1)

    Path(args.output).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"cost": result["cost"], "yield_verdicts": result["yield"]["verdicts"]}, indent=2))


if __name__ == "__main__":
    main()
