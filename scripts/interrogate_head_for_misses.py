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

# Half-height of the probe box. The benchmark tolerance is 1.0 km; 0.02 degrees
# of latitude is ~2.2 km, deliberately WIDER so a near-miss shows up as found
# rather than as ABSENT. Over-collecting here is safe -- the name test still
# has to pass -- while under-collecting would silently inflate ABSENT, which is
# the bucket that ends work rather than starting it.
PROBE_DEGREES = 0.02


def probe_box(lat: float, lon: float) -> tuple[float, float, float, float]:
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
        south, north, west, east = probe_box(lat, lon)
        probes.append({
            "case_id": row["case_id"],
            "query": row.get("query"),
            "expected_name": case.get("expected_name"),
            "alt_names": case.get("alt_names", []),
            "lat": lat, "lon": lon,
            "south": south, "north": north, "west": west, "east": east,
            "hit": bool(row.get("found_at_10")),
            "empty": not row.get("candidates"),
            "strata": row.get("strata", {}),
        })
    return probes


def name_hits(probe: dict, names: list[str]) -> list[str]:
    accepted = [n for n in (probe["expected_name"], *probe["alt_names"]) if n]
    matched = []
    for name in names:
        if not name:
            continue
        if any(normalize_name(name) == normalize_name(a) for a in accepted) or \
                any(_names_contain(name, a) for a in accepted):
            matched.append(name)
    return matched


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
        corpus_matched = name_hits(probe, corpus_names)
        head_matched = name_hits(probe, [n for n, _ in head_pairs])
        if head_matched:
            verdict = "IN_HEAD"
        elif corpus_matched:
            verdict = "NOT_ADMITTED"
        else:
            verdict = "ABSENT"
        verdicts.append({
            **{k: probe[k] for k in
               ("case_id", "query", "expected_name", "hit", "empty", "strata")},
            "verdict": verdict,
            "corpus_rows_near": len(corpus_names),
            "corpus_name_matches": corpus_matched[:3],
            "head_rows_near": len(head_pairs),
            "head_name_matches": head_matched[:3],
            "head_tokens": sorted({t for n, t in head_pairs
                                   if n in set(head_matched)})[:8],
        })

    misses = [v for v in verdicts if not v["hit"]]
    hits = [v for v in verdicts if v["hit"]]
    counts: dict[str, int] = {}
    for verdict in misses:
        counts[verdict["verdict"]] = counts.get(verdict["verdict"], 0) + 1

    print("\n=== MISSES by verdict ===")
    for name in ("ABSENT", "NOT_ADMITTED", "IN_HEAD"):
        count = counts.get(name, 0)
        share = 100 * count / len(misses) if misses else 0
        print(f"  {name:<14} {count:>4}  ({share:.1f}% of {len(misses)} misses)")

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
        "probe_degrees": PROBE_DEGREES,
        "cases": str(args.cases),
        "run": str(args.run),
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
