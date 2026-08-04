#!/usr/bin/env python3
"""Split benchmark misses into absent / not-admitted / retrievable, offline.

The everyday-POI benchmark says 106 of 130 misses return NOTHING, and
re-scoring proved that is a retrieval problem rather than a scoring artifact
(at most 5 cases were name-unscorable). What it could not say is WHICH
retrieval problem, because a deployed Worker answers "empty" identically for
three completely different causes:

  ABSENT          the entity is not in Overture near that point under that
                  name at all -- unwinnable, and it should stop being counted
                  as a failure
  NOT_ADMITTED    it IS in the source corpus but no head candidate was emitted
                  for it -- an admission-policy problem, and the only one of
                  the three that a v5 rebuild can fix
  IN_HEAD         a head candidate exists -- so the corpus and the index are
                  both fine and the defect is in serving or routing, fixable
                  fix-forward with no rebuild

Those three want completely different work, and guessing between them is how
the previous wave ended up scoped against a class with no winnable cases. This
answers it directly against local artifacts: the planet source parquet and the
planet head-candidate packs. No deploy, no rebuild, no live requests.

Honest limits, stated because they bound the conclusion:

* It probes head CANDIDATES, which are pre-merge. The merge then applies
  `result_cap` (10) per token, so a candidate can exist and still lose its slot.
  IN_HEAD therefore means "admitted", not "served"; cap eviction is a fourth
  cause folded into IN_HEAD and needs the merged head to separate.
* Matching is by proximity plus name, exactly like the benchmark scorer, so it
  inherits the scorer's blind spots. Containment matching is used, which is the
  more generous of the two available.
* A case counted ABSENT is absent *near the expected point under a matching
  name*. A badly geocoded gold coordinate would look identical.

Usage:
    interrogate_head_for_misses.py --cases CASES.json --run RUN.json \\
        --places 'PATH/theme=places/type=place/*.parquet' \\
        --head-candidates 'PATH/head-candidates/sha256/*.parquet' \\
        [--output REPORT.json]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from benchmark_v2_forward import _names_contain, normalize_name  # noqa: E402

SCHEMA = "head-miss-interrogation-v1"

KM_PER_DEGREE = 111.32

# The probe box is twice the case's own scoring tolerance, floored at 1 km.
# Deliberately WIDER than the tolerance so a near miss reads as found rather
# than as ABSENT: over-collecting is safe because the name test still has to
# pass, while under-collecting silently inflates ABSENT -- the bucket that ends
# work rather than starting it.
#
# A fixed box cannot serve both sets. The everyday-POI cases are all 1.0 km, but
# the gold set ranges 0.25 km to 25 km, and probing a 25 km locality case with a
# 2.2 km box would report ABSENT for something plainly present.
PROBE_TOLERANCE_MULTIPLE = 2.0
PROBE_MIN_KM = 1.0


def probe_degrees(tolerance_km: float | None) -> float:
    tolerance = max(tolerance_km or PROBE_MIN_KM, PROBE_MIN_KM)
    return PROBE_TOLERANCE_MULTIPLE * tolerance / KM_PER_DEGREE


def probe_box(lat: float, lon: float,
              tolerance_km: float | None = None) -> tuple[float, float, float, float]:
    PROBE_DEGREES = probe_degrees(tolerance_km)
    # Longitude degrees shrink with latitude; without the cosine term a probe
    # at high latitude would be far narrower in km than the same box at the
    # equator, again biasing toward ABSENT.
    scale = max(math.cos(math.radians(lat)), 0.01)
    dlon = PROBE_DEGREES / scale
    return lat - PROBE_DEGREES, lat + PROBE_DEGREES, lon - dlon, lon + dlon


def load_probes(cases_path: Path, run_path: Path) -> list[dict]:
    cases = {c["id"]: c for c in json.loads(cases_path.read_text())["cases"]}
    run = json.loads(run_path.read_text())
    probes = []
    for row in run["results"]:
        if row.get("provider") != "overture":
            continue
        case = cases.get(row["case_id"])
        if case is None or case.get("kind") != "place":
            continue
        lat, lon = case.get("expected_lat"), case.get("expected_lon")
        if lat is None or lon is None:
            continue
        south, north, west, east = probe_box(lat, lon, case.get("tolerance_km"))
        probes.append({
            "case_id": row["case_id"],
            "query": row.get("query"),
            "expected_name": case.get("expected_name"),
            "alt_names": case.get("alt_names", []),
            "lat": lat, "lon": lon,
            "south": south, "north": north, "west": west, "east": east,
            "expected_feature_type": case.get("expected_feature_type"),
            "tolerance_km": case.get("tolerance_km"),
            "hit": bool(row.get("found_at_10")),
            "empty": not row.get("candidates"),
            "strata": row.get("strata", {}),
        })
    return probes


def name_hits(probe: dict, names: list[str]) -> dict[str, list[str]]:
    """Matches split by strength AND by which accepted name they matched.

    Two independent distinctions, both load-bearing.

    STRENGTH. Exact-normalized equality is confident; containment is not,
    because it accepts an entity merely NAMED AFTER the target -- `Discovery
    Times Square` (a museum) matches the query `Times Square`, and `MoneyMax
    Pawnshop - Marsiling MRT Station` matches `MARSILING MRT STATION`.

    WHICH NAME. A case carries `expected_name` plus optional `alt_names`, and a
    match on an alt name does NOT mean the QUERIED string is indexed. Four Hong
    Kong cases were counted IN_HEAD on this basis: each is queried in Chinese
    while its `alt_names` carries the English name Overture actually holds, and
    the source has an English `names.primary` with a null `names.common`, so no
    Chinese string exists to index at all. Under the string the user typed those
    entities are ABSENT, and reporting them as indexed sent work to the wrong
    place.
    """
    queried = [n for n in (probe["expected_name"],) if n]
    alternates = [n for n in probe["alt_names"] if n]
    found = {"exact": [], "loose": [], "alt_exact": [], "alt_loose": []}
    for name in names:
        if not name:
            continue
        if any(normalize_name(name) == normalize_name(a) for a in queried):
            found["exact"].append(name)
        elif any(_names_contain(name, a) for a in queried):
            found["loose"].append(name)
        elif any(normalize_name(name) == normalize_name(a) for a in alternates):
            found["alt_exact"].append(name)
        elif any(_names_contain(name, a) for a in alternates):
            found["alt_loose"].append(name)
    return found


def classify(found: dict[str, list[str]]) -> tuple[bool, str]:
    """``(matched at all, strength label)`` for one field's matches."""
    if found["exact"]:
        return True, "EXACT"
    if found["loose"]:
        return True, "CONTAINMENT_ONLY"
    if found["alt_exact"]:
        return True, "ALT_NAME_ONLY"
    if found["alt_loose"]:
        return True, "ALT_NAME_CONTAINMENT"
    return False, "NONE"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--places", required=True,
                        help="glob of local Overture places parquet")
    parser.add_argument("--head-candidates", required=True,
                        help="glob of planet head-candidate packs")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    import duckdb

    probes = load_probes(args.cases, args.run)
    if not probes:
        raise SystemExit("no place probes built; wrong cases/run pairing?")
    print(f"probes {len(probes)} "
          f"({sum(p['hit'] for p in probes)} hits, "
          f"{sum(not p['hit'] for p in probes)} misses)", flush=True)

    con = duckdb.connect()
    con.execute("SET threads=16")
    con.execute("""
        CREATE TEMP TABLE probe(
            case_id VARCHAR, south DOUBLE, north DOUBLE,
            west DOUBLE, east DOUBLE)
    """)
    con.executemany(
        "INSERT INTO probe VALUES (?, ?, ?, ?, ?)",
        [(p["case_id"], p["south"], p["north"], p["west"], p["east"])
         for p in probes],
    )

    # Corpus: does a place exist near the point at all, and under what names?
    print("scanning planet places ...", flush=True)
    corpus_rows = con.execute(f"""
        SELECT p.case_id, s.names.primary AS name
        FROM probe p JOIN read_parquet('{args.places}') s
          ON s.bbox.ymin BETWEEN p.south AND p.north
         AND s.bbox.xmin BETWEEN p.west AND p.east
    """).fetchall()
    corpus: dict[str, list[str]] = {}
    for case_id, name in corpus_rows:
        corpus.setdefault(case_id, []).append(name)
    print(f"  {len(corpus_rows):,} corpus rows across "
          f"{len(corpus)} probes", flush=True)

    # Head candidates: was a head entry emitted for that feature?
    print("scanning head candidates ...", flush=True)
    head_rows = con.execute(f"""
        SELECT p.case_id, h.primary_name, h.token
        FROM probe p JOIN read_parquet('{args.head_candidates}') h
          ON h.latitude BETWEEN p.south AND p.north
         AND h.longitude BETWEEN p.west AND p.east
    """).fetchall()
    head: dict[str, list[tuple[str, str]]] = {}
    for case_id, name, token in head_rows:
        head.setdefault(case_id, []).append((name, token))
    print(f"  {len(head_rows):,} head rows across {len(head)} probes",
          flush=True)

    verdicts = []
    for probe in probes:
        corpus_names = corpus.get(probe["case_id"], [])
        head_pairs = head.get(probe["case_id"], [])
        corpus_found = name_hits(probe, corpus_names)
        head_found = name_hits(probe, [n for n, _ in head_pairs])
        head_hit, head_strength = classify(head_found)
        corpus_hit, corpus_strength = classify(corpus_found)
        if head_hit:
            verdict, strength = "IN_HEAD", head_strength
        elif corpus_hit:
            verdict, strength = "NOT_ADMITTED", corpus_strength
        else:
            verdict, strength = "ABSENT", "NONE"
        matched = head_found if head_hit else corpus_found
        names_matched = (matched["exact"] + matched["loose"]
                         + matched["alt_exact"] + matched["alt_loose"])
        verdicts.append({
            **{k: probe[k] for k in
               ("case_id", "query", "expected_name", "expected_feature_type",
                "tolerance_km", "hit", "empty", "strata")},
            "verdict": verdict,
            "match_strength": strength,
            "corpus_rows_near": len(corpus_names),
            "head_rows_near": len(head_pairs),
            "matched_names": names_matched[:3],
            "head_tokens": sorted({t for n, t in head_pairs
                                   if n in set(names_matched)})[:8],
        })

    # Locality cases are answered by the DIVISION lane, not the Places head.
    # Probing the head for them would report ABSENT for something plainly
    # present, so they are reported separately rather than folded in.
    division = [v for v in verdicts if v["expected_feature_type"] == "locality"]
    head_lane = [v for v in verdicts if v["expected_feature_type"] != "locality"]
    if division:
        print(f"\nexcluded {len(division)} locality cases "
              f"({sum(not v['hit'] for v in division)} of them misses): the "
              "division lane serves these, not the Places head")
    misses = [v for v in head_lane if not v["hit"]]
    hits = [v for v in head_lane if v["hit"]]
    counts: dict[str, int] = {}
    for verdict in misses:
        counts[verdict["verdict"]] = counts.get(verdict["verdict"], 0) + 1

    print("\n=== MISSES by verdict and match strength ===")
    for name in ("ABSENT", "NOT_ADMITTED", "IN_HEAD"):
        subset = [v for v in misses if v["verdict"] == name]
        share = 100 * len(subset) / len(misses) if misses else 0
        line = f"  {name:<14} {len(subset):>4}  ({share:.1f}%)"
        if name != "ABSENT":
            by: dict[str, int] = {}
            for v in subset:
                by[v["match_strength"]] = by.get(v["match_strength"], 0) + 1
            line += "   " + ", ".join(
                f"{key.lower()} {count}" for key, count in sorted(by.items()))
        print(line)

    # Only EXACT on the QUERIED name is confident. Everything else is a claim
    # needing eyes, and conflating them is how the first run overstated the
    # Worker-fixable class by roughly 4x.
    for label, keys in (
        ("containment only -- may be an entity NAMED AFTER the target",
         {"CONTAINMENT_ONLY"}),
        ("an ALT name, not the queried string -- the queried name may be absent",
         {"ALT_NAME_ONLY", "ALT_NAME_CONTAINMENT"}),
    ):
        weak = [v for v in misses
                if v["verdict"] != "ABSENT" and v["match_strength"] in keys]
        if weak:
            print(f"\n  {len(weak)} verdicts rest on {label}:")
            for v in weak[:6]:
                print(f"    {v['query'][:32]:<32} -> {v['matched_names'][:1]}")

    # Calibration: the same test run against cases the system DID solve. A test
    # that cannot find known-present entities is measuring its own blind spot,
    # and this is the number that says whether ABSENT can be trusted.
    hit_in_head = sum(1 for v in hits if v["verdict"] == "IN_HEAD")
    print(f"\ncalibration on {len(hits)} known hits: "
          f"{hit_in_head} IN_HEAD ({100 * hit_in_head / max(1, len(hits)):.1f}%)")
    if hits and hit_in_head / len(hits) < 0.9:
        print("  WARNING: the probe misses entities the system provably serves, "
              "so ABSENT is an upper bound, not a measurement.")

    report = {
        "schema": SCHEMA,
        "probe_tolerance_multiple": PROBE_TOLERANCE_MULTIPLE,
        "probe_min_km": PROBE_MIN_KM,
        "cases": str(args.cases),
        "run": str(args.run),
        "division_lane_cases": len(division),
        "misses": len(misses),
        "counts": counts,
        "calibration_hits": len(hits),
        "calibration_in_head": hit_in_head,
        "verdicts": verdicts,
    }
    if args.output:
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
        print(f"\nReport written to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
