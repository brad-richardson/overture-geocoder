#!/usr/bin/env python3
"""Measure what an Overture RELEASE MOVE does to benchmark recall, offline.

`docs/plans/2026-08-04-measurement-apparatus-findings.md` named the release
move from `2026-06-17.0` (what production serves) to `2026-07-22.0` "the
largest untested quality lever on the board".  It has never been benchmarked,
and the framing assumed it could only add.  The two local planet builds say
otherwise before a single case is probed: 06-17 carries 75,642,289 places and
07-22 carries 74,223,561 -- **1.4M fewer** -- while emitting MORE distinct head
index entries.  So the move is a redistribution, not an addition, and it can
lose cases as well as win them.

This probe answers both directions against local artifacts.  No deploy, no
rebuild, no live requests, no R2.

For each case, in EACH release independently, it assigns one verdict:

  ABSENT        no place near the expected point under a matching name
  NOT_ADMITTED  in the corpus, but no head candidate was emitted for it
  QUERY_REFUSED admitted, but the query exceeds HEAD_QUERY_TOKEN_CAP so the
                head lane declines it outright.  Release-invariant by
                construction, and reported so it cannot masquerade as a delta.
  EVICTED       admitted, but loses the per-token cap in at least one query
                word, so it is in no posting the query reads
  SERVABLE      admitted and survives the cap in EVERY query word

...and then diffs the two.  The EVICTED/SERVABLE split is the whole
methodological point.  `docs/plans/2026-08-04-head-miss-interrogation.md`
collapsed both into "IN_HEAD", published "the largest actionable class needs no
rebuild", and had to retract it: IN_HEAD means admitted, not served, and the
merge's cap sits between the two.  Splitting them is what makes a release delta
mean something.

Three controls, each bought with a specific error from 2026-08-04:

  * BOTH releases go through this same function in the same run.  The
    interrogation compared a July index against a June measurement and
    invalidated its own report; the only defence is to never compute the two
    sides differently.
  * EXACT-name and weak (containment / alt-name) tiers are kept apart.
    Containment accepted `Discovery Times Square` for `Times Square` and turned
    "the landmark is missing" into "the serving path is broken".  Only the
    EXACT tier is used for the headline delta.
  * Current HITS are probed too, not just misses.  With 1.4M records
    disappearing, a release move can lose what it does not gain, and a
    miss-only probe is structurally blind to that.

Usage:

    2026-08-04-release-move-recall-delta.py \\
        --cases benchmarks/everyday-poi-tripwire-cases-v1.json \\
        --run   benchmarks/2026-08-04-everyday-poi-post-additive-wave.json \\
        --label-a 2026-06-17.0 \\
        --places-a '/.../2026-06-17.0/theme=places/type=place/*.parquet' \\
        --head-candidates-a '/.../planet-0617/store/map/places-v1/head-candidates/sha256/*.parquet' \\
        --label-b 2026-07-22.0 \\
        --places-b '/.../2026-07-22.0/theme=places/type=place/*.parquet' \\
        --head-candidates-b '/.../planet/store/map/places-v1/head-candidates/sha256/*.parquet' \\
        --output benchmarks/2026-08-04-release-move-recall-delta-everyday.json

Limitations, stated up front because they bound every number below:

  * It models the HEAD TOKEN-INTERSECTION lane only.  The entity-phrase lane
    (`e2:`/`e3:`) and the prefix-head fallback can serve a record this probe
    calls EVICTED.  So SERVABLE is "reachable by the plain head path", not "the
    deployed Worker returns it", and the delta is a delta in that lane.
  * Ranks are computed over head CANDIDATES, which is exactly what the merge
    consumes, and the merge is a pure top-n over this key.  Candidate packs are
    ALREADY capped per map task, so `rank <= HEAD_RESULT_CAP` is exact -- that
    is the decomposability property the cap key is built on -- while a rank
    ABOVE the cap is a LOWER BOUND.  The SERVABLE/EVICTED split only ever tests
    `rank <= cap`, so it is sound; the printed magnitudes are not.
  * ABSENT means absent near the expected point under a matching name.  A badly
    geocoded gold coordinate looks identical.
  * Query tokenization mirrors the producer's word normalization; it is not the
    Worker's tokenizer executed in-process.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from benchmark_v2_forward import _names_contain, normalize_name  # noqa: E402

SCHEMA = "overture-places-release-move-recall-delta-v1"

# Frozen worker constants; see crates/geocoder-worker/src/places_construction_v1.rs.
HEAD_RESULT_CAP = 10
HEAD_QUERY_TOKEN_CAP = 3

KM_PER_DEGREE = 111.32
PROBE_TOLERANCE_MULTIPLE = 2.0
PROBE_MIN_KM = 1.0

# The cap key as one lexicographically-comparable hex string; segment widths are
# fixed so ordering the concatenation orders the tuple.  Inverted where CAP_ORDER
# sorts DESC.  Copied verbatim from the 2026-08-04 cap-eviction probe so the two
# measurements remain comparable.
#
#   IDENTIFYING_FIRST ((field_mask & 3) != 0) DESC
#   prominence_rank DESC, confidence_rank DESC
#   feature_id, source_object_index, source_row_group, source_row_index ASC
CAP_KEY_SQL = """
    printf('%01d%02X%02X',
           CASE WHEN (field_mask & 3) != 0 THEN 0 ELSE 1 END,
           255 - prominence_rank,
           255 - confidence_rank)
    || upper(hex(feature_id))
    || printf('%08X%08X%016X',
              source_object_index, source_row_group, source_row_index)
"""

# The verdict ladder, best to worst.  Order is load-bearing: a transition is a
# GAIN when it moves toward the front of this list.
VERDICTS = ["SERVABLE", "EVICTED", "QUERY_REFUSED", "NOT_ADMITTED", "ABSENT"]
VERDICT_ORDER = {name: index for index, name in enumerate(VERDICTS)}


def normalized_words(value: str) -> list[str]:
    """The producer's word normalization: NFKD, lower, strip marks, split."""
    folded = unicodedata.normalize("NFKD", value).lower()
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    folded = re.sub(r"[^\w]+", " ", folded, flags=re.UNICODE)
    return [word for word in folded.split() if word]


def probe_degrees(tolerance_km: float | None) -> float:
    return PROBE_TOLERANCE_MULTIPLE * max(
        tolerance_km or PROBE_MIN_KM, PROBE_MIN_KM) / KM_PER_DEGREE


def probe_box(lat: float, lon: float,
              tolerance_km: float | None) -> tuple[float, float, float, float]:
    degrees = probe_degrees(tolerance_km)
    # Longitude degrees shrink with latitude; without the cosine term a probe at
    # high latitude is far narrower in km than the same box at the equator,
    # biasing toward ABSENT -- the bucket that ends work rather than starting it.
    scale = max(math.cos(math.radians(lat)), 0.01)
    return lat - degrees, lat + degrees, lon - degrees / scale, lon + degrees / scale


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
        # Locality cases are answered by the DIVISION lane, not the Places head.
        # Probing the head for them manufactures ABSENT verdicts for things
        # plainly present.
        if case.get("expected_feature_type") == "locality":
            continue
        south, north, west, east = probe_box(lat, lon, case.get("tolerance_km"))
        probes.append({
            "case_id": row["case_id"],
            "query": row.get("query") or "",
            "expected_name": case.get("expected_name"),
            "alt_names": case.get("alt_names", []) or [],
            "south": south, "north": north, "west": west, "east": east,
            "tolerance_km": case.get("tolerance_km"),
            "production_hit": bool(row.get("found_at_10")),
            "strata": row.get("strata", {}),
            "query_words": normalized_words(row.get("query") or ""),
        })
    return probes


def name_hits(probe: dict, names) -> dict[str, list[str]]:
    """Matches split by strength AND by which accepted name they matched.

    A match on an alt name does NOT mean the QUERIED string is indexed.  Four
    Hong Kong cases were counted IN_HEAD on exactly that basis: each is queried
    in Chinese while its `alt_names` carries the English name Overture actually
    holds, so under the string the user typed they are absent.
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


def strength_of(found: dict[str, list[str]]) -> tuple[bool, str]:
    if found["exact"]:
        return True, "EXACT"
    if found["loose"]:
        return True, "CONTAINMENT_ONLY"
    if found["alt_exact"]:
        return True, "ALT_NAME_ONLY"
    if found["alt_loose"]:
        return True, "ALT_NAME_CONTAINMENT"
    return False, "NONE"


def measure_release(con, probes: list[dict], places_glob: str,
                    head_glob: str, label: str) -> dict[str, dict]:
    """Verdict per case for ONE release.  Both releases call this same code."""
    print(f"\n===== {label} =====", flush=True)
    con.execute("DROP TABLE IF EXISTS probe")
    con.execute("""
        CREATE TABLE probe(
            case_id VARCHAR, south DOUBLE, north DOUBLE,
            west DOUBLE, east DOUBLE)
    """)
    con.executemany("INSERT INTO probe VALUES (?,?,?,?,?)",
                    [(p["case_id"], p["south"], p["north"], p["west"], p["east"])
                     for p in probes])

    started = time.monotonic()
    print("  scanning source places ...", flush=True)
    corpus: dict[str, list[str]] = {}
    for case_id, name in con.execute(f"""
        SELECT p.case_id, s.names.primary AS name
        FROM probe p JOIN read_parquet('{places_glob}') s
          ON s.bbox.ymin BETWEEN p.south AND p.north
         AND s.bbox.xmin BETWEEN p.west AND p.east
    """).fetchall():
        corpus.setdefault(case_id, []).append(name)
    print(f"    {sum(len(v) for v in corpus.values()):,} corpus rows across "
          f"{len(corpus)} probes in {time.monotonic() - started:.1f}s", flush=True)

    # Head candidates near each point: admission AND the target's identity in
    # one pass.  Resolving identity separately would double the 15 GB scan.
    started = time.monotonic()
    print("  scanning head candidates (admission + identity) ...", flush=True)
    con.execute("DROP TABLE IF EXISTS admitted")
    con.execute(f"""
        CREATE TABLE admitted AS
        SELECT p.case_id, h.feature_id, h.primary_name,
               any_value(h.category) AS category,
               max(h.prominence_rank) AS prominence_rank,
               max(h.confidence_rank) AS confidence_rank,
               count(*) AS token_rows
        FROM probe p JOIN read_parquet('{head_glob}') h
          ON h.latitude BETWEEN p.south AND p.north
         AND h.longitude BETWEEN p.west AND p.east
        GROUP BY p.case_id, h.feature_id, h.primary_name
    """)
    admitted: dict[str, list[dict]] = {}
    for row in con.execute("""
        SELECT case_id, feature_id, primary_name, category,
               prominence_rank, confidence_rank, token_rows FROM admitted
    """).fetchall():
        admitted.setdefault(row[0], []).append({
            "feature_id": row[1], "primary_name": row[2], "category": row[3],
            "prominence_rank": row[4], "confidence_rank": row[5],
            "token_rows": row[6],
        })
    print(f"    {sum(len(v) for v in admitted.values()):,} distinct features "
          f"across {len(admitted)} probes in "
          f"{time.monotonic() - started:.1f}s", flush=True)

    # Classify admission, and pick the target feature to rank.  Preference is
    # by name strength first (an EXACT name identifies the target; a containment
    # match may be a different business named after it), then by how many tokens
    # the record carries, then by feature_id for determinism.
    state: dict[str, dict] = {}
    rankable: list[tuple[str, bytes, list[str]]] = []
    for probe in probes:
        case_id = probe["case_id"]
        features = admitted.get(case_id, [])
        head_found = name_hits(probe, [f["primary_name"] for f in features])
        head_hit, head_strength = strength_of(head_found)
        corpus_found = name_hits(probe, corpus.get(case_id, []))
        corpus_hit, corpus_strength = strength_of(corpus_found)

        target = None
        if head_hit:
            accepted = {n for key in ("exact", "loose", "alt_exact", "alt_loose")
                        for n in head_found[key]}
            best_key = ("exact", "loose", "alt_exact", "alt_loose")
            ranked_names = {n: i for i, key in enumerate(best_key)
                            for n in head_found[key]}
            candidates = [f for f in features if f["primary_name"] in accepted]
            candidates.sort(key=lambda f: (ranked_names[f["primary_name"]],
                                           -f["token_rows"], bytes(f["feature_id"])))
            target = candidates[0] if candidates else None

        if head_hit:
            verdict, strength = "ADMITTED", head_strength
        elif corpus_hit:
            verdict, strength = "NOT_ADMITTED", corpus_strength
        else:
            verdict, strength = "ABSENT", "NONE"

        state[case_id] = {
            "verdict": verdict, "match_strength": strength,
            "corpus_rows_near": len(corpus.get(case_id, [])),
            "features_near": len(features),
            "target": None if target is None else {
                "primary_name": target["primary_name"],
                "category": target["category"],
                "prominence_rank": target["prominence_rank"],
                "confidence_rank": target["confidence_rank"],
                "feature_uuid": bytes(target["feature_id"]).hex(),
            },
            "tokens": [],
        }
        if verdict == "ADMITTED":
            if target is None:
                # A name matched but no feature resolved -- possible only if the
                # accepted-name set and the feature list disagree, which they
                # cannot today since both derive from the same rows. Treated as
                # not-admitted rather than left as a verdict outside the ladder,
                # so a future change surfaces as a conservative count instead of
                # a KeyError in the diff.
                state[case_id]["verdict"] = "NOT_ADMITTED"
            elif len(probe["query_words"]) > HEAD_QUERY_TOKEN_CAP:
                state[case_id]["verdict"] = "QUERY_REFUSED"
            else:
                rankable.append((case_id, target["feature_id"],
                                 probe["query_words"]))

    print(f"    admitted {sum(1 for s in state.values() if s['verdict'] in ('ADMITTED', 'QUERY_REFUSED'))}, "
          f"not_admitted {sum(1 for s in state.values() if s['verdict'] == 'NOT_ADMITTED')}, "
          f"absent {sum(1 for s in state.values() if s['verdict'] == 'ABSENT')}", flush=True)

    # Rank each admitted target inside every token its query would look up.
    tokens = sorted({w for _, _, words in rankable for w in words})
    if not tokens:
        print("    nothing rankable", flush=True)
        return state

    started = time.monotonic()
    print(f"  ranking {len(rankable)} targets over {len(tokens)} tokens ...",
          flush=True)
    con.execute("DROP TABLE IF EXISTS query_token")
    con.execute("CREATE TABLE query_token(token VARCHAR)")
    con.executemany("INSERT INTO query_token VALUES (?)", [(t,) for t in tokens])
    con.execute("DROP TABLE IF EXISTS contender")
    con.execute(f"""
        CREATE TABLE contender AS
        SELECT h.token, h.feature_id, ({CAP_KEY_SQL}) AS cap_key
        FROM read_parquet('{head_glob}') h
        JOIN query_token q ON q.token = h.token
    """)
    con.execute("CREATE INDEX contender_token ON contender(token)")
    contention = {
        row[0]: row[1] for row in
        con.execute("SELECT token, count(*) FROM contender GROUP BY token").fetchall()
    }
    print(f"    {sum(contention.values()):,} contender rows in "
          f"{time.monotonic() - started:.1f}s", flush=True)

    for case_id, feature_id, words in rankable:
        per_token = []
        for token in words:
            row = con.execute(
                "SELECT cap_key FROM contender WHERE token = ? AND feature_id = ?"
                " ORDER BY cap_key LIMIT 1", [token, feature_id]).fetchone()
            if row is None:
                per_token.append({
                    "token": token, "rank": None, "survives": False,
                    "reason": "ABSENT_FROM_TOKEN",
                    "contenders": contention.get(token, 0),
                })
                continue
            rank = con.execute(
                "SELECT count(*) + 1 FROM contender WHERE token = ? AND cap_key < ?",
                [token, row[0]]).fetchone()[0]
            per_token.append({
                "token": token, "rank": rank,
                "survives": rank <= HEAD_RESULT_CAP,
                "reason": None if rank <= HEAD_RESULT_CAP else "EVICTED_BY_RANK",
                "contenders": contention.get(token, 0),
            })
        state[case_id]["tokens"] = per_token
        # The head INTERSECTS per-token top-n lists, so the record must survive
        # in EVERY queried word.  One lost token empties the intersection.
        state[case_id]["verdict"] = (
            "SERVABLE" if all(t["survives"] for t in per_token) else "EVICTED")

    for name in VERDICTS:
        count = sum(1 for s in state.values() if s["verdict"] == name)
        if count:
            print(f"    {name:<14} {count}", flush=True)
    return state


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--label-a", required=True)
    parser.add_argument("--places-a", required=True)
    parser.add_argument("--head-candidates-a", required=True)
    parser.add_argument("--label-b", required=True)
    parser.add_argument("--places-b", required=True)
    parser.add_argument("--head-candidates-b", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--threads", type=int, default=16)
    parser.add_argument("--memory-limit", default="32GB")
    args = parser.parse_args(argv)

    import duckdb

    probes = load_probes(args.cases, args.run)
    if not probes:
        raise SystemExit("no place probes built; wrong cases/run pairing?")
    hits = sum(p["production_hit"] for p in probes)
    print(f"{len(probes)} head-lane place cases "
          f"({hits} production hits, {len(probes) - hits} misses)", flush=True)

    con = duckdb.connect()
    con.execute(f"SET threads={args.threads}")
    con.execute(f"SET memory_limit='{args.memory_limit}'")
    con.execute(f"SET temp_directory='{args.output.parent}/.duckdb-tmp'")

    side_a = measure_release(con, probes, args.places_a,
                             args.head_candidates_a, args.label_a)
    side_b = measure_release(con, probes, args.places_b,
                             args.head_candidates_b, args.label_b)

    # Diff.  A transition is scored only on the EXACT tier; weak tiers are
    # carried but excluded from the headline, because containment accepts an
    # entity merely NAMED AFTER the target.
    cases = []
    for probe in probes:
        case_id = probe["case_id"]
        a, b = side_a[case_id], side_b[case_id]
        exact_tier = (a["match_strength"] in ("EXACT", "NONE")
                      and b["match_strength"] in ("EXACT", "NONE"))
        movement = VERDICT_ORDER[b["verdict"]] - VERDICT_ORDER[a["verdict"]]
        cases.append({
            "case_id": case_id,
            "query": probe["query"],
            "expected_name": probe["expected_name"],
            "strata": probe["strata"],
            "production_hit": probe["production_hit"],
            "exact_tier": exact_tier,
            "a": a, "b": b,
            "transition": f"{a['verdict']}->{b['verdict']}",
            "direction": ("GAIN" if movement < 0 else
                          "LOSS" if movement > 0 else "SAME"),
        })

    matrix: dict[str, int] = {}
    for case in cases:
        matrix[case["transition"]] = matrix.get(case["transition"], 0) + 1

    def tally(subset):
        return {
            "cases": len(subset),
            "gain": sum(1 for c in subset if c["direction"] == "GAIN"),
            "loss": sum(1 for c in subset if c["direction"] == "LOSS"),
            "same": sum(1 for c in subset if c["direction"] == "SAME"),
            "servable_a": sum(1 for c in subset if c["a"]["verdict"] == "SERVABLE"),
            "servable_b": sum(1 for c in subset if c["b"]["verdict"] == "SERVABLE"),
        }

    exact = [c for c in cases if c["exact_tier"]]
    summary = {
        "all_cases": tally(cases),
        "exact_tier_only": tally(exact),
        "production_hits": tally([c for c in exact if c["production_hit"]]),
        "production_misses": tally([c for c in exact if not c["production_hit"]]),
    }
    by_country: dict[str, dict] = {}
    for case in exact:
        key = str(case["strata"].get("country") or "?")
        by_country.setdefault(key, []).append(case)
    strata_summary = {k: tally(v) for k, v in sorted(by_country.items())}

    evidence = {
        "schema": SCHEMA,
        "cases_file": str(args.cases),
        "run_file": str(args.run),
        "release_a": args.label_a,
        "release_b": args.label_b,
        "head_result_cap": HEAD_RESULT_CAP,
        "head_query_token_cap": HEAD_QUERY_TOKEN_CAP,
        "summary": summary,
        "transition_matrix": dict(sorted(matrix.items())),
        "by_country_exact_tier": strata_summary,
        "cases": cases,
        "limitations": [
            "Models the HEAD TOKEN-INTERSECTION lane only. The entity-phrase "
            "lane (e2:/e3:) and the prefix-head fallback can serve a record "
            "this probe calls EVICTED, so SERVABLE means 'reachable by the "
            "plain head path', not 'the deployed Worker returns it'.",
            "Candidate packs are ALREADY capped per map task, so rank <= "
            "HEAD_RESULT_CAP is exact while a rank above it is a LOWER BOUND. "
            "The SERVABLE/EVICTED split only tests rank <= cap, so the split is "
            "sound; the printed rank magnitudes are not.",
            "QUERY_REFUSED is release-invariant by construction and can never "
            "produce a delta; it is reported so it cannot masquerade as one.",
            "ABSENT means absent near the expected point under a matching name. "
            "A badly geocoded gold coordinate looks identical.",
            "Weak (containment / alt-name) tiers are carried but excluded from "
            "the headline delta.",
        ],
    }
    args.output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")

    print(f"\n=== {args.label_a} -> {args.label_b} ===")
    for name, value in summary.items():
        print(f"  {name:<20} {json.dumps(value, sort_keys=True)}")
    print("\n=== transitions (all tiers) ===")
    for key, count in sorted(matrix.items(), key=lambda kv: -kv[1]):
        mark = "" if key.split("->")[0] == key.split("->")[1] else "   <-- MOVED"
        print(f"  {key:<34} {count:>4}{mark}")
    print(f"\nwrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
