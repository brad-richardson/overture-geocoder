#!/usr/bin/env python3
"""Build the natural-feature stratum: gold from Wikidata, controls from Overture.

`2026-08-07-base-theme-landmark-import-scope.md` §6.3 says the theme=base import
cannot be justified on the four incremental gold landmarks alone, and that the
classes it would really add -- peaks, islands, bridges, plazas, parks -- are
untested by either frozen set. This builds that stratum, instrument first, the
same order the proximity wave followed.

GOLD IS INDEPENDENT OF BOTH SIDES. Names and coordinates come from Wikidata,
which is neither the system under test (Overture) nor a compared provider
(Nominatim and Photon derive from OpenStreetMap). Every case records its QID so
the claim is checkable.

Each case carries TWO offline controls, which is what makes the stratum able to
size the import rather than merely score it:

  * `control_places`  -- does the Places corpus production serves hold a
    name-matching record within tolerance? (Is it already servable in principle?)
  * `control_base`    -- does the proposed base admission set hold one?
    (Would the import supply it?)

Together with the live result, those separate three different failures: the
entity is absent from Overture entirely, it is present in base only (the import
is the fix), or it is present in Places and the index/ranking is the fix.

Sampling: for each (class, country) the endpoint's first `--pool` rows are
fetched, then ranked by sha256 of the QID and the top `--per-country` taken.
That is deterministic given the same pool, and the pool is not guaranteed stable
across Wikidata edits -- which is why the output case file is frozen and
committed rather than rebuilt per run.

Usage:
  .venv/bin/python benchmarks/probes/2026-08-07-build-natural-feature-cases.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import requests

REPO = Path(__file__).resolve().parents[2]
ENDPOINT = "https://qlever.cs.uni-freiburg.de/api/wikidata"
USER_AGENT = (
    "overture-geocoder-benchmark/1.0 "
    "(https://github.com/brad-richardson/overture-geocoder; gold collection)"
)

# Feature classes the base import would add, with the tolerance each deserves.
# A peak is a summit point; an island is an extent whose Wikidata coordinate is
# a centroid, so it needs room.
CLASSES = {
    "peak": {"qid": "Q8502", "tolerance_km": 2.0},
    "volcano": {"qid": "Q8072", "tolerance_km": 5.0},
    "island": {"qid": "Q23442", "tolerance_km": 10.0},
    "bridge": {"qid": "Q12280", "tolerance_km": 1.0},
    "square": {"qid": "Q174782", "tolerance_km": 1.0},
    "park": {"qid": "Q22698", "tolerance_km": 2.0},
}

COUNTRIES = [
    "US", "GB", "FR", "DE", "IT", "ES", "JP", "KR", "TW", "SG",
    "AU", "CA", "MX", "BR", "IN", "ZA", "NO", "PL", "TR", "HK",
]

PLACES = "/home/brad/dev/overture-local/2026-06-17.0/theme=places/type=place/*.parquet"
BASE_ADMITTED = "/home/brad/dev/overture-local/base-admitted/*.parquet"

POINT = re.compile(r"POINT\(([-0-9.eE]+) ([-0-9.eE]+)\)")


def sparql(query: str, retries: int = 4) -> list[dict]:
    for attempt in range(retries):
        response = requests.get(
            ENDPOINT,
            params={"query": query},
            headers={
                "Accept": "application/sparql-results+json",
                "User-Agent": USER_AGENT,
            },
            timeout=180,
        )
        if response.status_code == 200:
            return response.json()["results"]["bindings"]
        time.sleep(5 * (attempt + 1))
    raise SystemExit(f"SPARQL failed after {retries} attempts: {response.status_code}")


def fetch_pool(class_qid: str, country: str, pool: int) -> list[dict]:
    rows = sparql(
        f"""
        PREFIX wdt: <http://www.wikidata.org/prop/direct/>
        PREFIX wd: <http://www.wikidata.org/entity/>
        PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
        SELECT ?item ?label ?coord WHERE {{
          ?item wdt:P31 wd:{class_qid} .
          ?item wdt:P625 ?coord .
          ?item wdt:P17 ?country .
          ?country wdt:P297 "{country}" .
          ?item rdfs:label ?label .
          FILTER(LANG(?label) = "en")
        }} LIMIT {pool}
        """
    )
    out = []
    for row in rows:
        match = POINT.match(row["coord"]["value"])
        if not match:
            continue
        qid = row["item"]["value"].rsplit("/", 1)[-1]
        label = row["label"]["value"].strip('"')
        if not label or label.startswith("Q") and label[1:].isdigit():
            continue
        out.append(
            {
                "qid": qid,
                "label": label,
                "lon": float(match.group(1)),
                "lat": float(match.group(2)),
                "country": country,
            }
        )
    return out


def controls(cases: list[dict]) -> None:
    """Attach the two offline presence controls, one DuckDB pass each."""
    con = duckdb.connect(config={"memory_limit": "40GB", "threads": "16"})
    con.execute(
        "create table wanted (case_id varchar, name varchar, lat double, "
        "lon double, tolerance_km double)"
    )
    con.executemany(
        "insert into wanted values (?, ?, ?, ?, ?)",
        [
            (case["id"], case["expected_name"], case["expected_lat"],
             case["expected_lon"], case["tolerance_km"])
            for case in cases
        ],
    )
    # Normalized-name equality plus a haversine inside the case tolerance. The
    # bbox prefilter keeps this a scan rather than a cross join.
    corpora = (
        (
            "control_places",
            f"""select names.primary as name,
                       (bbox.xmin + bbox.xmax) / 2 as lon,
                       (bbox.ymin + bbox.ymax) / 2 as lat
                from read_parquet('{PLACES}')
                where names.primary is not null and names.primary <> ''""",
        ),
        (
            "control_base",
            f"""select name, lon, lat from read_parquet('{BASE_ADMITTED}')
                where name is not null and name <> ''""",
        ),
    )
    for label, corpus_sql in corpora:
        started = time.monotonic()
        rows = con.execute(
            f"""
            with corpus as ({corpus_sql})
            select w.case_id, count(*) as hits
            from wanted w join corpus c
              on lower(strip_accents(c.name)) = lower(strip_accents(w.name))
             -- A degree box first so this stays a scan with a cheap filter; the
             -- haversine below is what actually decides the tolerance.
             and c.lat between w.lat - 0.15 and w.lat + 0.15
             and c.lon between w.lon - 0.15 / greatest(cos(radians(w.lat)), 0.05)
                          and w.lon + 0.15 / greatest(cos(radians(w.lat)), 0.05)
             and 6371.0088 * 2 * asin(sqrt(
                   pow(sin(radians(c.lat - w.lat) / 2), 2)
                   + cos(radians(w.lat)) * cos(radians(c.lat))
                     * pow(sin(radians(c.lon - w.lon) / 2), 2))) <= w.tolerance_km
            group by 1
            """
        ).fetchall()
        hits = {case_id: count for case_id, count in rows}
        for case in cases:
            case[label] = hits.get(case["id"], 0)
        print(f"  {label}: {sum(1 for c in cases if c[label])}/{len(cases)} "
              f"({time.monotonic() - started:.0f}s)", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--per-country", type=int, default=1)
    parser.add_argument("--pool", type=int, default=120)
    parser.add_argument(
        "--output", default=str(REPO / "benchmarks/natural-feature-cases-v1.json")
    )
    args = parser.parse_args()

    accessed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cases: list[dict] = []
    for class_name, spec in CLASSES.items():
        for country in COUNTRIES:
            pool = fetch_pool(spec["qid"], country, args.pool)
            if not pool:
                continue
            pool.sort(key=lambda row: hashlib.sha256(row["qid"].encode()).hexdigest())
            for row in pool[: args.per_country]:
                cases.append(
                    {
                        "id": f"natural:{class_name}:{row['qid']}",
                        "kind": "place",
                        "query": row["label"],
                        "query_style": "natural_feature",
                        "expected_name": row["label"],
                        "expected_lat": row["lat"],
                        "expected_lon": row["lon"],
                        "tolerance_km": spec["tolerance_km"],
                        "expected_feature_type": "poi",
                        "strata": {
                            "scope": "natural_feature",
                            "feature_class": class_name,
                            "country": country,
                        },
                        "provenance": {
                            "source_name": "Wikidata",
                            "source_kind": "open_knowledge_base",
                            "source_license": "CC0-1.0",
                            "source_record_id": row["qid"],
                            "instance_of": spec["qid"],
                            "coordinate_property": "P625",
                            "selection_method": (
                                f"first {args.pool} endpoint rows for "
                                f"(class, country), ranked by sha256(QID), "
                                f"top {args.per_country}"
                            ),
                            "accessed_at": accessed_at,
                            "osm_derived": False,
                        },
                    }
                )
            print(f"  {class_name:8} {country}: pool {len(pool):>4}", flush=True)

    print(f"{len(cases)} cases; attaching offline controls", flush=True)
    controls(cases)

    document = {
        "schema": "benchmark-v2-forward-cases-v1",
        "collection_status": {
            "purpose": (
                "Size the theme=base import on the classes it would actually "
                "add. Gold is independent of Overture and of both compared "
                "providers; the two controls separate an absent entity from an "
                "unindexed one."
            ),
            "controls": {
                "control_places": (
                    "count of name-matching Places records within tolerance, "
                    "Overture 2026-06-17.0 -- already servable in principle"
                ),
                "control_base": (
                    "count of name-matching records within tolerance in the "
                    "proposed base admission set -- what the import would add"
                ),
            },
            "caveats": [
                "Queries are English Wikidata labels. A local-language query is "
                "a different measurement this stratum does not make.",
                "Coordinates are Wikidata P625: a summit for a peak, a centroid "
                "for an island, which is why tolerance varies by class.",
                "The endpoint pool is not stable across Wikidata edits, so this "
                "file is frozen rather than rebuilt per run.",
            ],
        },
        "cases": cases,
    }
    Path(args.output).write_text(json.dumps(document, indent=2) + "\n")
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
