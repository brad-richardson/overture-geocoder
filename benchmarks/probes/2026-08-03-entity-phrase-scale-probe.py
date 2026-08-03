#!/usr/bin/env python3
"""Size a category-gated exact-name phrase lane on bounded metro regions.

The lane under test emits one synthetic global-head key for a two- or
three-word primary name when the existing category prior is non-zero.  It does
not claim that category means fame; the category is only a hard, already-bound
admission budget.  Exact phrase equality supplies the identity evidence.

The probe compares that proposal with the deliberately unsafe upper bound of
emitting a phrase for every admitted name.  It reads seven small metropolitan
bboxes from the live build's exact Overture source release and does not write
remote state.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

import duckdb


ROOT = Path(__file__).resolve().parents[2]
RELEASE = "2026-06-17.0"
SOURCE = (
    "s3://overturemaps-us-west-2/release/"
    f"{RELEASE}/theme=places/type=place/*"
)
REGIONS = {
    "paris_fr": (2.20, 48.80, 2.40, 48.92),
    "berlin_de": (13.30, 52.45, 13.50, 52.57),
    "tokyo_jp": (139.65, 35.63, 139.85, 35.75),
    "mumbai_in": (72.80, 18.90, 73.00, 19.15),
    "lagos_ng": (3.30, 6.40, 3.50, 6.60),
    "sao_paulo_br": (-46.75, -23.65, -46.55, -23.50),
    "seattle_us": (-122.45, 47.50, -122.20, 47.72),
}
HEAD_RESULT_CAP = 10
# Actual 2026-08-02 planet output: 5,141,583,720 bytes / 30,841,082 rows.
# This is a deliberately conservative whole-lane estimate because it charges
# every new row the existing head's index+payload average.
PLANET_HEAD_BYTES_PER_RECORD = 5_141_583_720 / 30_841_082
PLANET_ADMITTED_PLACE_ROWS = 75_631_061


def _load_type_prior():
    path = ROOT / "scripts/places_type_prior_v1.py"
    spec = importlib.util.spec_from_file_location("phrase_probe_type_prior", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TYPE_PRIOR = _load_type_prior()


def sql_values(values) -> str:
    return ", ".join("'" + value.replace("'", "''") + "'" for value in sorted(values))


def measure_region(con: duckdb.DuckDBPyConnection, bbox: tuple[float, ...]) -> dict:
    landmark = sql_values(TYPE_PRIOR.LANDMARK_PRIOR)
    commodity = sql_values(TYPE_PRIOR.COMMODITY_CATEGORIES)
    x0, y0, x1, y1 = bbox
    row = con.execute(
        f"""
        WITH source_rows AS (
          SELECT
            trim(regexp_replace(
              lower(strip_accents(names.primary)),
              '[^\\p{{L}}\\p{{N}}_]+', ' ', 'g'
            )) AS phrase,
            categories.primary AS category,
            categories.alternate AS alternates,
            basic_category
          FROM read_parquet('{SOURCE}', hive_partitioning=true)
          WHERE bbox.xmin BETWEEN ? AND ?
            AND bbox.ymin BETWEEN ? AND ?
            AND names.primary IS NOT NULL
            AND COALESCE(operating_status, 'open') != 'permanently_closed'
        ), tagged AS (
          SELECT *,
            len(regexp_split_to_array(phrase, '\\s+')) AS token_count,
            (
              NOT (
                category IN ({commodity}) OR basic_category IN ({commodity})
              )
              AND (
                category IN ({landmark}) OR basic_category IN ({landmark})
                OR list_has_any(
                  COALESCE(alternates, []::VARCHAR[]),
                  [{landmark}]
                )
              )
            ) AS prominent
          FROM source_rows
          WHERE phrase != ''
        ), phrase_groups AS (
          SELECT
            phrase,
            count(*) AS all_records,
            count(*) FILTER (WHERE prominent) AS prominent_records
          FROM tagged
          WHERE token_count BETWEEN 2 AND 3
          GROUP BY phrase
        )
        SELECT
          (SELECT count(*) FROM tagged) AS admitted_rows,
          COALESCE(sum(all_records), 0) AS all_phrase_input_records,
          count(*) AS all_phrase_keys,
          COALESCE(sum(least(all_records, {HEAD_RESULT_CAP})), 0)
            AS all_phrase_retained_records,
          COALESCE(sum(prominent_records), 0) AS prominent_phrase_input_records,
          count(*) FILTER (WHERE prominent_records > 0) AS prominent_phrase_keys,
          COALESCE(sum(least(prominent_records, {HEAD_RESULT_CAP})), 0)
            AS prominent_phrase_retained_records,
          max(all_records) AS largest_all_phrase_posting,
          max(prominent_records) AS largest_prominent_phrase_posting
        FROM phrase_groups
        """,
        [x0, x1, y0, y1],
    ).fetchone()
    columns = [item[0] for item in con.description]
    result = dict(zip(columns, row, strict=True))
    for key, value in tuple(result.items()):
        result[key] = int(value or 0)
    result["prominent_retained_pct_of_admitted"] = round(
        100 * result["prominent_phrase_retained_records"] / result["admitted_rows"],
        3,
    )
    result["all_retained_pct_of_admitted"] = round(
        100 * result["all_phrase_retained_records"] / result["admitted_rows"], 3
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--only-region", action="append", choices=sorted(REGIONS))
    args = parser.parse_args()

    selected = args.only_region or list(REGIONS)
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2'")
    regions = {}
    for index, name in enumerate(selected, 1):
        print(f"[{index}/{len(selected)}] {name}", flush=True)
        regions[name] = measure_region(con, REGIONS[name])

    totals = {
        key: sum(region[key] for region in regions.values())
        for key in (
            "admitted_rows",
            "all_phrase_input_records",
            "all_phrase_keys",
            "all_phrase_retained_records",
            "prominent_phrase_input_records",
            "prominent_phrase_keys",
            "prominent_phrase_retained_records",
        )
    }
    totals["prominent_retained_pct_of_admitted"] = round(
        100 * totals["prominent_phrase_retained_records"] / totals["admitted_rows"],
        3,
    )
    totals["all_retained_pct_of_admitted"] = round(
        100 * totals["all_phrase_retained_records"] / totals["admitted_rows"], 3
    )
    totals["prominent_estimated_bytes"] = round(
        totals["prominent_phrase_retained_records"] * PLANET_HEAD_BYTES_PER_RECORD
    )
    totals["all_estimated_bytes"] = round(
        totals["all_phrase_retained_records"] * PLANET_HEAD_BYTES_PER_RECORD
    )
    prominent_ratio = (
        totals["prominent_phrase_retained_records"] / totals["admitted_rows"]
    )
    totals["directional_planet_prominent_records"] = round(
        prominent_ratio * PLANET_ADMITTED_PLACE_ROWS
    )
    totals["directional_planet_prominent_bytes"] = round(
        totals["directional_planet_prominent_records"]
        * PLANET_HEAD_BYTES_PER_RECORD
    )
    evidence = {
        "schema": "overture-places-entity-phrase-scale-v1",
        "source_release": RELEASE,
        "head_result_cap": HEAD_RESULT_CAP,
        "phrase_contract": {
            "field": "primary_name",
            "token_count": [2, 3],
            "normalization": "bounded SQL approximation of nfkd-lower-stripmark-cjk-bigram-v4",
            "admission": "existing prominence_rank > 0",
            "key_limit_per_record": 1,
        },
        "estimation_basis": {
            "planet_head_bytes": 5_141_583_720,
            "planet_head_records": 30_841_082,
            "planet_admitted_place_rows": PLANET_ADMITTED_PLACE_ROWS,
            "bytes_per_record": round(PLANET_HEAD_BYTES_PER_RECORD, 3),
        },
        "regions": regions,
        "totals": totals,
        "limitations": [
            "Seven metro bboxes are not a planet projection.",
            "SQL normalization is sufficient for Latin-script sizing but is not the frozen Rust tokenizer oracle.",
            "The bytes estimate charges the current head average and is directional, not an encoded artifact measurement.",
        ],
    }
    Path(args.output).write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    print(json.dumps(totals, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
