#!/usr/bin/env python3
"""Classify everyday-POI benchmark misses by failure mechanism.

The everyday-POI tripwire measures recall but not *why* a case missed. This
script joins a frozen Overture run to the frozen case set, the OSM/Overpass
presence control, and a frozen external (Nominatim/Photon) baseline, and
attributes every miss to a mechanism.

The distinction that matters for build sizing is:

  * empty response, query >= 4 tokens  -> blocked by HEAD_QUERY_TOKEN_CAP
    before any index read (query-side; a Worker fallback can reach it)
  * empty response, query <= 3 tokens  -> the query reached the index and
    still starved (admission/coverage; only a rebuild can reach it)
  * non-empty response                 -> retrieval worked, ranking or the
    entity itself is wrong

Each bucket is then cross-tabbed against independent evidence that the entity
exists in open data at all (exact/fuzzy OSM name within 500 m, or a competitor
hit). A miss with no such evidence is NOT demonstrably our failure, and must
not be counted as addressable upside.

Usage:
  python scripts/classify_everyday_poi_misses.py \
      --run benchmarks/2026-08-04-everyday-poi-post-v4.json \
      --cases benchmarks/everyday-poi-tripwire-cases-v1.json \
      --presence benchmarks/2026-08-03-everyday-poi-overpass-presence-v1.json \
      --external benchmarks/2026-08-03-everyday-poi-external-baseline-v1.json \
      --output benchmarks/2026-08-04-everyday-poi-miss-classification-v1.json
"""

from __future__ import annotations

import argparse
import collections
import json
import re

# Mirrors crates/geocoder-worker/src/places_construction_v1.rs HEAD_QUERY_TOKEN_CAP.
# A no-proximity query with more tokens than this returns empty before any read.
HEAD_QUERY_TOKEN_CAP = 3

PRESENCE_NAME_EVIDENCE = ("exact_name_present", "fuzzy_name_candidate")
EXTERNAL_PROVIDERS = ("nominatim", "photon")


def tokens(query):
    return [t for t in re.split(r"[\s　]+", query.strip()) if t]


def load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def classify(run, cases, presence, external):
    case_by_id = {case["id"]: case for case in cases["cases"]}
    presence_by_id = {row["case_id"]: row for row in presence["results"]}
    external_by_key = {
        (row["provider"], row["case_id"]): row for row in external["results"]
    }

    rows = [r for r in run["results"] if r["provider"] == "overture"]
    decisions = []
    for row in rows:
        case_id = row["case_id"]
        n_tokens = len(tokens(row["query"]))
        empty = row.get("top1_feature_type") is None
        hit10 = bool(row.get("found_at_10"))

        presence_status = presence_by_id.get(case_id, {}).get("status")
        competitor_hit = any(
            bool(external_by_key.get((provider, case_id), {}).get("found_at_10"))
            for provider in EXTERNAL_PROVIDERS
        )
        open_data_evidence = (
            presence_status in PRESENCE_NAME_EVIDENCE or competitor_hit
        )

        if hit10:
            mechanism = "hit"
        elif not empty:
            mechanism = "returned_wrong_entity"
        elif n_tokens > HEAD_QUERY_TOKEN_CAP:
            mechanism = "empty_token_cap"
        else:
            mechanism = "empty_index_starved"

        decisions.append(
            {
                "case_id": case_id,
                "query": row["query"],
                "query_tokens": n_tokens,
                "rank": row.get("rank"),
                "found_at_10": hit10,
                "empty_response": empty,
                "mechanism": mechanism,
                "osm_presence_status": presence_status,
                "competitor_hit": competitor_hit,
                "open_data_name_evidence": open_data_evidence,
                "addressable": mechanism != "hit" and open_data_evidence,
                "strata": case_by_id.get(case_id, {}).get("strata", {}),
            }
        )
    return decisions


def summarize(decisions):
    by_mechanism = collections.Counter(d["mechanism"] for d in decisions)
    misses = [d for d in decisions if d["mechanism"] != "hit"]

    cross = collections.defaultdict(lambda: {"total": 0, "with_evidence": 0})
    for decision in misses:
        bucket = cross[decision["mechanism"]]
        bucket["total"] += 1
        if decision["open_data_name_evidence"]:
            bucket["with_evidence"] += 1

    by_country = collections.defaultdict(
        lambda: {"n": 0, "misses": 0, "empty_token_cap": 0, "empty_index_starved": 0}
    )
    for decision in decisions:
        country = decision["strata"].get("country", "unknown")
        entry = by_country[country]
        entry["n"] += 1
        if decision["mechanism"] != "hit":
            entry["misses"] += 1
        if decision["mechanism"] in entry:
            entry[decision["mechanism"]] += 1

    return {
        "cases": len(decisions),
        "hits_at_10": by_mechanism.get("hit", 0),
        "misses": len(misses),
        "by_mechanism": dict(sorted(by_mechanism.items())),
        "mechanism_vs_open_data_evidence": {k: dict(v) for k, v in sorted(cross.items())},
        "addressable_misses": sum(1 for d in misses if d["open_data_name_evidence"]),
        "unevidenced_misses": sum(
            1 for d in misses if not d["open_data_name_evidence"]
        ),
        "by_country": {k: dict(v) for k, v in sorted(by_country.items())},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True)
    parser.add_argument("--cases", required=True)
    parser.add_argument("--presence", required=True)
    parser.add_argument("--external", required=True)
    parser.add_argument("--output")
    args = parser.parse_args()

    run = load(args.run)
    decisions = classify(run, load(args.cases), load(args.presence), load(args.external))
    summary = summarize(decisions)

    artifact = {
        "schema": "everyday-poi-miss-classification-v1",
        "inputs": {
            "run": args.run,
            "cases": args.cases,
            "presence": args.presence,
            "external": args.external,
            "run_timestamp": run.get("meta", {}).get("timestamp"),
            "run_data_version": run.get("meta", {}).get("data_version"),
        },
        "head_query_token_cap": HEAD_QUERY_TOKEN_CAP,
        "summary": summary,
        "decisions": decisions,
    }

    print(json.dumps(summary, indent=2, ensure_ascii=False))
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            json.dump(artifact, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        print(f"\nwrote {args.output}")


if __name__ == "__main__":
    main()
