#!/usr/bin/env python3
"""Measure how much of the forward gold set lives in Overture's `base` theme.

The geocoder indexes `places` and `divisions` only.  A spot check on 2026-08-04
found Golden Gate Bridge in `base/infrastructure` (wikidata `Q44440`,
`names.common` in 11 languages) and Times Square in `base/land_use`
(`Q11259`, 9 languages) -- both at correct coordinates, and both entirely
absent from `places`.  That is a coverage gap no amount of ranking work could
close, and it lands on exactly the class the gold set is made of.

Two things make `base` more than a coverage patch:

  * it carries `wikidata` as a first-class column, so entity fame needs no
    GERS-to-QID matcher and none of the sidecar's hand adjudication; and
  * `names.common` is POPULATED here, where every `places` record checked had
    it empty -- an in-corpus fame proxy and a fix for non-Latin queries.

This turns that spot check into a number: for each gold POI case, is the
expected entity present in `base` near the expected point under a matching
name, and does it carry a QID and multilingual names?

It reads the release parquet REMOTELY over HTTPS with bbox pruning.  There is
no download: the three scanned types are ~61 GB and the answer needs a few
thousand rows.

NOT scanned, and this bounds the result: `theme=buildings` is 276 GB, which is
not viable to scan this way.  Several gold landmarks (Eiffel Tower, Empire
State Building, Sagrada Familia, Buckingham Palace, Big Ben) are plausibly
buildings rather than infrastructure, so a case reported ABSENT_FROM_BASE here
may simply be in the theme this probe cannot afford to read.  Read the absent
bucket as "not in the three scanned base types", never as "not in Overture".

Usage:

    2026-08-05-gold-coverage-in-base-theme.py \\
        --cases benchmarks/v2-forward-gold-cases-v1.json \\
        --release 2026-06-17.0 \\
        --places-verdicts benchmarks/2026-08-04-release-move-recall-delta-gold.json \\
        --output benchmarks/2026-08-05-gold-coverage-in-base-theme.json
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from benchmark_v2_forward import _names_contain, normalize_name  # noqa: E402

SCHEMA = "overture-gold-coverage-in-base-theme-v1"
BUCKET = "overturemaps-us-west-2"
HTTP_ROOT = f"https://{BUCKET}.s3.us-west-2.amazonaws.com/release"
S3_ROOT = f"s3://{BUCKET}/release"

# land_cover is huge and carries no named entities; water carries none of this
# gold set; buildings is 276 GB. See the module docstring.
BASE_TYPES = ("infrastructure", "land_use", "land")

KM_PER_DEGREE = 111.32
PROBE_TOLERANCE_MULTIPLE = 2.0
PROBE_MIN_KM = 1.0


def probe_box(lat: float, lon: float, tolerance_km: float | None):
    degrees = PROBE_TOLERANCE_MULTIPLE * max(
        tolerance_km or PROBE_MIN_KM, PROBE_MIN_KM) / KM_PER_DEGREE
    scale = max(math.cos(math.radians(lat)), 0.01)
    return lat - degrees, lat + degrees, lon - degrees / scale, lon + degrees / scale


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    radius = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def parquet_list(release: str, theme: str, type_: str) -> str:
    listing = subprocess.run(
        ["aws", "s3", "ls", "--no-sign-request",
         f"{S3_ROOT}/{release}/theme={theme}/type={type_}/"],
        capture_output=True, text=True, check=True).stdout.split()
    names = [n for n in listing if n.endswith(".parquet")]
    if not names:
        raise SystemExit(f"no parquet listed for {theme}/{type_}")
    return "[" + ",".join(
        f"'{HTTP_ROOT}/{release}/theme={theme}/type={type_}/{n}'" for n in names) + "]"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--release", default="2026-06-17.0")
    parser.add_argument("--places-verdicts", type=Path,
                        help="release-move delta JSON, to cross-tab what base ADDS")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--threads", type=int, default=16)
    args = parser.parse_args(argv)

    import duckdb

    cases = [c for c in json.loads(args.cases.read_text())["cases"]
             if c.get("kind") == "place"
             and c.get("expected_feature_type") != "locality"
             and c.get("expected_lat") is not None]
    print(f"{len(cases)} gold POI cases", flush=True)

    boxes = {}
    for case in cases:
        south, north, west, east = probe_box(
            case["expected_lat"], case["expected_lon"], case.get("tolerance_km"))
        boxes[case["id"]] = (south, north, west, east)

    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute(f"SET threads={args.threads}")

    where = " OR ".join(
        f"(bbox.ymin BETWEEN {s} AND {n} AND bbox.xmin BETWEEN {w} AND {e})"
        for s, n, w, e in boxes.values())

    rows_by_type: dict[str, list] = {}
    for type_ in BASE_TYPES:
        files = parquet_list(args.release, "base", type_)
        started = time.monotonic()
        print(f"scanning base/{type_} ...", flush=True)
        rows = con.execute(f"""
            SELECT names.primary AS nm, subtype, class,
                   wikidata,
                   coalesce(len(map_keys(names.common)), 0) AS n_common,
                   (bbox.ymin + bbox.ymax) / 2 AS lat,
                   (bbox.xmin + bbox.xmax) / 2 AS lon
            FROM read_parquet({files})
            WHERE ({where}) AND names.primary IS NOT NULL
        """).fetchall()
        rows_by_type[type_] = rows
        print(f"  {len(rows):,} named rows in {time.monotonic() - started:.0f}s",
              flush=True)

    places_verdict = {}
    if args.places_verdicts and args.places_verdicts.exists():
        for row in json.loads(args.places_verdicts.read_text())["cases"]:
            places_verdict[row["case_id"]] = {
                "a": row["a"]["verdict"], "production_hit": row["production_hit"]}

    results = []
    for case in cases:
        south, north, west, east = boxes[case["id"]]
        accepted = [case.get("expected_name")] + list(case.get("alt_names") or [])
        accepted = [a for a in accepted if a]
        best = None
        for type_, rows in rows_by_type.items():
            for nm, subtype, class_, qid, n_common, lat, lon in rows:
                if not (south <= lat <= north and west <= lon <= east):
                    continue
                if any(normalize_name(nm) == normalize_name(a) for a in accepted):
                    strength = "EXACT"
                elif any(_names_contain(nm, a) for a in accepted):
                    strength = "CONTAINMENT"
                else:
                    continue
                candidate = {
                    "name": nm, "type": f"base/{type_}", "subtype": subtype,
                    "class": class_, "wikidata": qid, "names_common": n_common,
                    "match_strength": strength,
                    "distance_km": round(haversine_km(
                        case["expected_lat"], case["expected_lon"], lat, lon), 3),
                }
                # Prefer an exact name, then a QID, then more languages, then closer.
                key = (strength != "EXACT", qid is None, -n_common,
                       candidate["distance_km"])
                if best is None or key < best[0]:
                    best = (key, candidate)
        results.append({
            "case_id": case["id"],
            "expected_name": case.get("expected_name"),
            "tolerance_km": case.get("tolerance_km"),
            "base": None if best is None else best[1],
            "places": places_verdict.get(case["id"]),
        })

    found = [r for r in results if r["base"]]
    exact_all = [r for r in found if r["base"]["match_strength"] == "EXACT"]
    # A transit stop named after a landmark is not the landmark.
    exact = [r for r in exact_all if r["base"]["subtype"] != "transit"]
    transit = [r for r in exact_all if r["base"]["subtype"] == "transit"]
    with_qid = [r for r in exact if r["base"]["wikidata"]]
    with_common = [r for r in exact if r["base"]["names_common"] > 0]
    # What base ADDS: cases Places cannot serve by the plain head path.
    adds = [r for r in exact
            if (r["places"] or {}).get("a") not in (None, "SERVABLE")]

    summary = {
        "gold_poi_cases": len(cases),
        "found_in_base_any": len(found),
        "found_in_base_exact_name": len(exact),
        "exact_but_transit_named_after": len(transit),
        "exact_with_wikidata_qid": len(with_qid),
        "exact_with_names_common": len(with_common),
        "exact_not_servable_from_places": len(adds),
        "scanned_types": [f"base/{t}" for t in BASE_TYPES],
    }
    evidence = {
        "schema": SCHEMA,
        "release": args.release,
        "cases_file": str(args.cases),
        "summary": summary,
        "cases": results,
        "limitations": [
            "theme=buildings (276 GB) was NOT scanned. Several gold landmarks are "
            "plausibly buildings, so an absent verdict means 'not in the three "
            "scanned base types', never 'not in Overture'.",
            "base/water and base/land_cover were not scanned: no gold case is a "
            "water or land-cover feature.",
            "A `transit` match is almost always a station or stop NAMED AFTER "
            "the landmark rather than the landmark itself (Colosseo metro, the "
            "Louvre metro station, a Harrods bus stop). Those are counted in a "
            "separate tier, never in the landmark headline -- this is the same "
            "failure mode that forced the Discovery Times Square retraction. "
            "Union Station is the case where a transit match is the RIGHT "
            "answer, so the tier is reported, not dropped.",
            "Coordinates are bbox centroids. For a long feature like a bridge the "
            "centroid is the right point; for a sprawling land_use polygon it may "
            "sit far from the entrance a user means.",
            "Presence in base is not servability: nothing here indexes base, and "
            "the producer changes to do so are not scoped by this probe.",
        ],
    }
    args.output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")

    print("\n=== gold coverage in base ===")
    for key, value in summary.items():
        print(f"  {key:<34} {value}")
    print(f"\n=== transit matches EXCLUDED from the headline ({len(transit)}) ===")
    for row in sorted(transit, key=lambda r: r["expected_name"] or ""):
        print(f"  {str(row['expected_name'])[:26]:<27} {str(row['base']['name'])[:26]:<27}"
              f" qid={str(row['base']['wikidata'])}")
    print("\n=== exact-name matches, non-transit ===")
    for row in sorted(exact, key=lambda r: r["expected_name"] or ""):
        base = row["base"]
        places = (row["places"] or {}).get("a", "?")
        print(f"  {str(row['expected_name'])[:26]:<27} {base['type']:<21}"
              f" {str(base['subtype'])[:11]:<12} qid={str(base['wikidata']):<11}"
              f" common={base['names_common']:<3} {base['distance_km']:>7.3f} km"
              f"   places={places}")
    missing = [r for r in results if not r["base"]]
    print(f"\n=== not found in scanned base types ({len(missing)}) ===")
    print("  " + ", ".join(sorted(str(r["expected_name"]) for r in missing)))
    print(f"\nwrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
