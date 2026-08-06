#!/usr/bin/env python3
"""Re-baseline the everyday-POI denominator by quarantining ABSENT cases.

Recommendation 1 of docs/plans/2026-08-06-places-failure-mode-review.md: the
everyday-POI headline (recall 0.345/0.350 over 200 cases) is dominated by
cases for which the bounded exact/containment probe found no matching Overture
name near the gold point — often government-registry legal names that map data
does not carry. Scoring all of those as ordinary misses overweights the gold
source, but ABSENT is not proof of nonexistence and the sensitivity bound below
must travel with the re-baseline.

This probe consumes ONLY committed evidence — zero live requests:

- benchmarks/2026-08-04-everyday-poi-post-additive-wave.json
  (the latest frozen run: 200 results against build 2026-08-03.0)
- benchmarks/2026-08-04-everyday-head-miss-interrogation-v1.json
  (per-case corpus verdicts: ABSENT / NOT_ADMITTED / IN_HEAD, from DuckDB
  name probes over the production-vintage 2026-06-17.0 corpus)

Quarantine rule: a case is quarantined when its interrogation verdict is
ABSENT **and** production did not hit it. A production hit is direct proof of
existence, so the 2 ABSENT-verdict hits stay in the denominator (the ABSENT
probe is an exact/containment name test and can be blind to a name form the
serving path matches).

Output: benchmarks/2026-08-06-everyday-denominator-rebaseline-v1.json with
the quarantine list, both scoreboards, and per-country deltas.

Usage: python benchmarks/probes/2026-08-06-everyday-denominator-rebaseline.py
"""

import json
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RUN_PATH = REPO / "benchmarks/2026-08-04-everyday-poi-post-additive-wave.json"
INTERROGATION_PATH = (
    REPO / "benchmarks/2026-08-04-everyday-head-miss-interrogation-v1.json"
)
OUTPUT_PATH = (
    REPO / "benchmarks/2026-08-06-everyday-denominator-rebaseline-v1.json"
)


def scoreboard(rows):
    n = len(rows)
    found1 = sum(bool(r["found_at_1"]) for r in rows)
    found10 = sum(bool(r["found_at_10"]) for r in rows)
    return {
        "n": n,
        "found_at_1": found1,
        "found_at_10": found10,
        "recall_at_1": round(found1 / n, 3) if n else None,
        "recall_at_10": round(found10 / n, 3) if n else None,
    }


def by_country(rows):
    grouped = {}
    for row in rows:
        grouped.setdefault(row["strata"]["country"], []).append(row)
    return {country: scoreboard(group)
            for country, group in sorted(grouped.items())}


def main():
    run = json.loads(RUN_PATH.read_text())
    interrogation = json.loads(INTERROGATION_PATH.read_text())

    verdicts = {v["case_id"]: v for v in interrogation["verdicts"]}
    results = [r for r in run["results"] if r["provider"] == "overture"]
    assert len(results) == 200, f"expected 200 results, got {len(results)}"
    assert set(verdicts) == {r["case_id"] for r in results}, (
        "interrogation verdicts and run results cover different case sets"
    )

    quarantined, kept, absent_but_hit = [], [], []
    for row in results:
        verdict = verdicts[row["case_id"]]
        if verdict["verdict"] == "ABSENT" and not row["found_at_10"]:
            quarantined.append(row)
        else:
            kept.append(row)
            if verdict["verdict"] == "ABSENT":
                absent_but_hit.append(row["case_id"])

    old = scoreboard(results)
    corrected = scoreboard(kept)
    false_absent_rate = len(absent_but_hit) / old["found_at_10"]
    scenario_false_quarantines = round(
        len(quarantined) * false_absent_rate)
    scenario_rows = kept + quarantined[:scenario_false_quarantines]
    scenario = scoreboard(scenario_rows)
    kept_concentration_countries = {"HK", "TW", "SG", "MX", "JP"}
    kept_concentration = sum(
        1 for row in kept
        if row["strata"]["country"] in kept_concentration_countries
    )
    payload = {
        "schema": "everyday-denominator-rebaseline-v1",
        "inputs": {
            "run": str(RUN_PATH.relative_to(REPO)),
            "interrogation": str(INTERROGATION_PATH.relative_to(REPO)),
            "build": run["meta"]["data_version"],
        },
        "quarantine_rule": (
            "verdict == ABSENT (no name-matching Overture record within "
            "2x tolerance of the gold point, min 1 km, on the production "
            "2026-06-17.0 corpus) AND production did not hit the case; "
            "a production hit proves existence and overrides ABSENT"
        ),
        "upper_bound_note": (
            "The corrected 0.639/0.648 rates are upper bounds, not a simple "
            "denominator correction: ABSENT is a bounded name probe and was "
            "wrong for 2 of the 70 production hits (2.9%). Applying that "
            "observed rate to 92 quarantines restores about 3 misses; the "
            "illustrative equal-blind-rate scenario is approximately "
            "0.622/0.631 (n=111). This is not an empirical false-quarantine "
            "estimate: it extrapolates a rate observed among hits to misses."
        ),
        "absent_probe_equal_blind_rate_scenario": {
            "absent_verdict_hits": len(absent_but_hit),
            "production_hits_at_10": old["found_at_10"],
            "observed_false_absent_rate_among_hits": round(false_absent_rate, 3),
            "scenario_false_quarantines": scenario_false_quarantines,
            "scenario_scoreboard": scenario,
            "method_caveat": (
                "Illustrative only: assumes the ABSENT-probe blind rate among "
                "production misses equals the 2/70 rate observed among hits."
            ),
        },
        "population_shift_note": (
            "Quarantine is not missing at random. CO falls 20->0 cases, AU "
            "25->7, and KR 20->7; 94/108 kept cases (87%) are from HK, TW, "
            "SG, MX, or JP. The corrected rate therefore describes a "
            "materially different country population from the original 200."
        ),
        "kept_concentration": {
            "countries": sorted(kept_concentration_countries),
            "cases": kept_concentration,
            "share": round(kept_concentration / len(kept), 3),
        },
        "quarantined_count": len(quarantined),
        "kept_count": len(kept),
        "absent_verdict_but_production_hit": absent_but_hit,
        "verdict_counts_all": dict(
            Counter(v["verdict"] for v in verdicts.values())
        ),
        "old_scoreboard": old,
        "corrected_scoreboard": corrected,
        "old_by_country": by_country(results),
        "corrected_by_country": by_country(kept),
        "quarantined_case_ids": sorted(r["case_id"] for r in quarantined),
        "quarantined_by_country": dict(
            Counter(r["strata"]["country"] for r in quarantined)
        ),
    }
    OUTPUT_PATH.write_text(
        json.dumps(payload, indent=1, ensure_ascii=False) + "\n"
    )

    print(f"quarantined {len(quarantined)} / 200 cases "
          f"(ABSENT and missed); kept {len(kept)}")
    if absent_but_hit:
        print(f"ABSENT verdict but production hit (kept): {absent_but_hit}")
    print(f"old headline:       r@1 {old['recall_at_1']}  "
          f"r@10 {old['recall_at_10']}  (n={old['n']})")
    print(f"corrected headline: r@1 {corrected['recall_at_1']}  "
          f"r@10 {corrected['recall_at_10']}  (n={corrected['n']})")
    print(f"quarantine by country: {payload['quarantined_by_country']}")
    print(f"wrote {OUTPUT_PATH.relative_to(REPO)}")


if __name__ == "__main__":
    main()
