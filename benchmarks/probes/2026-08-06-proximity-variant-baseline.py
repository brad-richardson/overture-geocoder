#!/usr/bin/env python3
"""Run + score the proximity and variant-typing strata against production.

Wraps scripts/benchmark_v2_forward.py (Runner, scoring helpers) rather than
reimplementing transport: requests are sequential, paced, retried only on
definite transients, and every response's candidate list is retained so this
run stays re-scorable offline.

This probe retains the stock runner's transport and candidate evidence while
adding the two measurements recommendation 1 of
docs/plans/2026-08-06-places-failure-mode-review.md asks for and the stock
scorer cannot express. Proximity rows discard the stock exact-anchor score
because the deliberately displaced construction anchor is not the gold:

- proximity cases ("chain near me"): the rank of any candidate whose name
  permissively contains the chain tokens
  within 2 km of the proximity point (not just the sampled anchor), and the
  distance of the top-1 result from the proximity point, with the assertion
  requiring both a chain-name match and top1 <= nearest_within_km;
- variant cases: each case is executed twice, once with the typed-variant
  query and once with control_query (the corpus spelling), so "the record is
  not retrievable at all" is separable from "the variant spelling breaks it".

Frozen request budget: the original 40 proximity + 20 variant + 20 control
run used 80 GETs. The two targeted hyphen cases added four GETs, for 84 total,
sequential at >= 0.4 s spacing against https://geocoder.bradr.dev.

Use --append-missing after extending a case file. It preserves the frozen
responses, executes only new variant/control pairs, and re-summarizes all
rows. It deliberately refuses new proximity cases because those require a
fresh, explicitly budgeted baseline.

Usage:
  python benchmarks/probes/2026-08-06-proximity-variant-baseline.py \
      [--base-url https://geocoder.bradr.dev] [--interval 0.4] \
      [--output benchmarks/2026-08-06-proximity-variant-baseline-v1.json]
"""

import argparse
import hashlib
import importlib.util
import json
import statistics
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PROXIMITY_CASES = REPO / "benchmarks/proximity-chain-cases-v1.json"
VARIANT_CASES = REPO / "benchmarks/variant-typing-cases-v1.json"
DEFAULT_OUTPUT = REPO / "benchmarks/2026-08-06-proximity-variant-baseline-v1.json"
INITIAL_RUN_GIT_SHA = "ed9b7770437e09cd19da8d1dbe9a4f0903a62364"
EXTENSION_RUN_BASE_GIT_SHA = "479d46c4f1d4e35a21d4c56f80e5c1eb1fae4787"

# Only fields that can change the request or custom score belong in this
# digest. Descriptive provenance/strata may be corrected without pretending a
# request was rerun; query, point, gold, tolerance, and scorer inputs may not.
CASE_FINGERPRINT_FIELDS = (
    "id", "kind", "query", "query_style", "expected_name", "alt_names",
    "expected_gers_id", "expected_lat", "expected_lon", "tolerance_km",
    "expected_feature_type", "control_query", "proximity",
    "proximity_assert",
)

# The stock Runner scores proximity cases against the displaced construction
# anchor. That anchor is non-nearest in 38/40 and the case contract forbids
# retaining or comparing this score. Keep transport/candidates, custom `prox`,
# and request metadata; remove every stock score/type derivative.
STOCK_SCORE_FIELDS = {
    "scoring_mode", "capability", "capability_reason", "rank", "found_at_1",
    "found_at_10", "matched_distance_km", "top1_distance_km",
    "expected_feature_type", "top1_feature_type", "type_at_1", "type_present",
}

spec = importlib.util.spec_from_file_location(
    "benchmark_v2_forward", REPO / "scripts/benchmark_v2_forward.py")
bvf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bvf)


def chain_name_matches(chain, candidate_name):
    """Permissive chain-name match, not proof of chain identity.

    Token containment intentionally credits names such as "Starbucks Reserve
    Roastery". It can also flatter the metric: the frozen Sydney case credits
    "Woolworths Riley Street Car Park". Interpret this as a conservative
    retrieval baseline, not entity-resolution ground truth.
    """
    if not candidate_name:
        return False
    if bvf.normalize_name(candidate_name) == bvf.normalize_name(chain):
        return True
    chain_tokens = bvf.name_tokens(chain)
    return bool(chain_tokens) and chain_tokens <= bvf.name_tokens(candidate_name)


def score_proximity(case, result):
    prox_lon, prox_lat = case["proximity"]
    chain = case["expected_name"]
    chain_rank_within = chain_rank_any = None
    top1_distance = None
    top1_is_chain = None
    for index, cand in enumerate(result.get("candidates", [])):
        distance = None
        if cand.get("lat") is not None and cand.get("lon") is not None:
            distance = bvf.haversine_km(
                prox_lat, prox_lon, cand["lat"], cand["lon"])
        is_chain = chain_name_matches(chain, cand.get("name"))
        if index == 0:
            top1_distance = distance
            top1_is_chain = is_chain
        if is_chain:
            if chain_rank_any is None:
                chain_rank_any = index + 1
            if (chain_rank_within is None and distance is not None
                    and distance <= case["proximity_assert"]["nearest_within_km"]):
                chain_rank_within = index + 1
    return {
        "chain_rank_within_2km": chain_rank_within,
        "chain_rank_any": chain_rank_any,
        "top1_distance_from_proximity_km": (
            None if top1_distance is None else round(top1_distance, 3)),
        "top1_is_chain": top1_is_chain,
        "top1_within_assert": (
            bool(top1_is_chain)
            and top1_distance is not None
            and top1_distance <= case["proximity_assert"]["nearest_within_km"]),
        "empty": not result.get("candidates"),
    }


def score_variant(case, result):
    """Rank of a candidate whose name equals the corpus spelling near the
    expected point (exact normalized equality; the query differs from the
    name by construction, the RESULT must not)."""
    rank = None
    for index, cand in enumerate(result.get("candidates", [])):
        if not cand.get("name"):
            continue
        if bvf.normalize_name(cand["name"].split(",")[0]) != \
                bvf.normalize_name(case["expected_name"]):
            continue
        if cand.get("lat") is None or cand.get("lon") is None:
            continue
        distance = bvf.haversine_km(
            case["expected_lat"], case["expected_lon"],
            cand["lat"], cand["lon"])
        if distance <= case["tolerance_km"]:
            rank = index + 1
            break
    return {
        "name_distance_rank": rank,
        "exact_id_rank": result.get("rank"),
        "empty": not result.get("candidates"),
    }


def rate(part, whole):
    return round(part / whole, 3) if whole else None


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def file_sha256(path):
    return sha256_bytes(path.read_bytes())


def case_fingerprint(case):
    projection = {
        key: case[key] for key in CASE_FINGERPRINT_FIELDS if key in case
    }
    encoded = json.dumps(
        projection, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return sha256_bytes(encoded)


def fingerprint_block(proximity_cases, variant_cases):
    return {
        "algorithm": "sha256-canonical-request-score-projection-v1",
        "proximity": {
            case["id"]: case_fingerprint(case) for case in proximity_cases
        },
        "variant": {
            case["id"]: case_fingerprint(case) for case in variant_cases
        },
    }


def request_count_provenance(append_missing, prior_meta, requests_this_run,
                             total_requests, run_timestamp):
    """Separate an initial run's requests from later append-only extensions."""
    if not append_missing:
        return {
            "initial_requests": total_requests,
            "extension_requests": 0,
            "requests_this_run": requests_this_run,
            "initial_timestamp": run_timestamp,
        }

    prior_extension_requests = prior_meta.get(
        "extension_requests", prior_meta.get("requests_added", 0))
    return {
        "initial_requests": prior_meta.get(
            "initial_requests",
            prior_meta.get("requests", 0) - prior_extension_requests,
        ),
        "extension_requests": prior_extension_requests + requests_this_run,
        "requests_this_run": requests_this_run,
        "initial_timestamp": prior_meta.get(
            "initial_timestamp", prior_meta.get("timestamp")),
    }


def scrub_stock_proximity_score(row):
    for field in STOCK_SCORE_FIELDS:
        row.pop(field, None)
    for candidate in row.get("candidates", []):
        # Runner computes this from the deliberately displaced construction
        # anchor. Raw coordinates remain available for custom proximity score
        # replay, so retaining the anchor-derived distance is both unnecessary
        # and contrary to the case contract.
        candidate.pop("distance_km", None)
    row["scoring_authority"] = "custom_chain_within_proximity_v1"


def validate_frozen_rows(proximity_cases, proximity_rows,
                         variant_cases, variant_pairs):
    proximity_by_id = {case["id"]: case for case in proximity_cases}
    for row in proximity_rows:
        case = proximity_by_id[row["case_id"]]
        if row.get("query") != case["query"]:
            raise ValueError(f"stale proximity query for {case['id']}")
        if row.get("proximity") != case["proximity"]:
            raise ValueError(f"stale proximity point for {case['id']}")
        rescored = score_proximity(case, row)
        if rescored != row.get("prox"):
            raise ValueError(f"stale proximity score for {case['id']}")
        # Descriptive strata do not affect request or score identity and may be
        # corrected offline. Keep the frozen row aligned with the current case
        # instead of silently preserving stale aggregation labels.
        row["strata"] = case["strata"]

    variants_by_id = {case["id"]: case for case in variant_cases}
    for pair in variant_pairs:
        case = variants_by_id[pair["case_id"]]
        if pair.get("query") != case["query"]:
            raise ValueError(f"stale variant query for {case['id']}")
        if pair.get("control_query") != case["control_query"]:
            raise ValueError(f"stale control query for {case['id']}")
        for side in ("variant", "control"):
            rescored = score_variant(case, pair[side])
            if rescored != pair[side].get("variant_score"):
                raise ValueError(f"stale {side} score for {case['id']}")
            pair[side]["strata"] = case["strata"]


def atomic_write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, indent=1, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def summarize_proximity(rows):
    def block(group):
        n = len(group)
        within1 = sum(r["prox"]["chain_rank_within_2km"] == 1 for r in group)
        within10 = sum(
            r["prox"]["chain_rank_within_2km"] is not None for r in group)
        any10 = sum(r["prox"]["chain_rank_any"] is not None for r in group)
        top1_ok = sum(bool(r["prox"]["top1_within_assert"]) for r in group)
        distances = [r["prox"]["top1_distance_from_proximity_km"]
                     for r in group
                     if r["prox"]["top1_distance_from_proximity_km"] is not None]
        return {
            "n": n,
            "chain_within_2km_at_1": within1,
            "chain_within_2km_at_10": within10,
            "rank1_rate": rate(within1, n),
            "rank10_rate": rate(within10, n),
            "chain_anywhere_at_10": any10,
            "top1_within_2km": top1_ok,
            "top1_within_2km_rate": rate(top1_ok, n),
            "empty_responses": sum(r["prox"]["empty"] for r in group),
            "median_top1_distance_km": (
                round(statistics.median(distances), 3) if distances else None),
            "max_top1_distance_km": (
                round(max(distances), 3) if distances else None),
        }

    by_country = {}
    for row in rows:
        by_country.setdefault(row["strata"]["country"], []).append(row)
    return {
        "overall": block(rows),
        "by_country": {country: block(group)
                       for country, group in sorted(by_country.items())},
    }


def summarize_variants(pairs):
    def block(group):
        n = len(group)
        control_hit = sum(
            p["control"]["variant_score"]["name_distance_rank"] is not None
            for p in group)
        variant_hit = sum(
            p["variant"]["variant_score"]["name_distance_rank"] is not None
            for p in group)
        both = sum(
            p["control"]["variant_score"]["name_distance_rank"] is not None
            and p["variant"]["variant_score"]["name_distance_rank"] is not None
            for p in group)
        return {
            "n": n,
            "control_hit_at_10": control_hit,
            "variant_hit_at_10": variant_hit,
            "both_hit": both,
            "control_only": control_hit - both,
            "variant_only": variant_hit - both,
            "neither": n - control_hit - (variant_hit - both),
            "control_hit_rate": rate(control_hit, n),
            "variant_hit_rate": rate(variant_hit, n),
        }

    by_class = {}
    for pair in pairs:
        by_class.setdefault(
            pair["variant"]["strata"]["variant_class"], []).append(pair)
    return {
        "overall": block(pairs),
        "by_class": {name: block(group)
                     for name, group in sorted(by_class.items())},
    }


def execute_variant_pair(runner, case):
    variant_result = runner.execute(case)
    if variant_result.get("error"):
        raise RuntimeError(
            f"variant request failed for {case['id']}: {variant_result['error']}"
        )
    variant_result["variant_score"] = score_variant(case, variant_result)
    variant_result["strata"] = case["strata"]
    control_case = {
        key: value for key, value in case.items()
        if key != "control_query"
    }
    control_case = dict(control_case,
                        id=case["id"] + ":control",
                        query=case["control_query"],
                        query_style=case["query_style"] + "_control")
    control_result = runner.execute(control_case)
    if control_result.get("error"):
        raise RuntimeError(
            f"control request failed for {case['id']}: {control_result['error']}"
        )
    control_result["variant_score"] = score_variant(case, control_result)
    control_result["strata"] = case["strata"]
    return {
        "case_id": case["id"],
        "query": case["query"],
        "control_query": case["control_query"],
        "variant": variant_result,
        "control": control_result,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--base-url", default=bvf.DEFAULT_BASE_URL)
    parser.add_argument("--interval", type=float, default=0.4)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--append-missing", action="store_true",
        help="preserve frozen rows and execute only newly added variant pairs",
    )
    parser.add_argument(
        "--migrate-frozen", action="store_true",
        help=(
            "offline one-time migration: rescore all frozen rows, bind case "
            "fingerprints, and scrub forbidden stock proximity scores"
        ),
    )
    args = parser.parse_args()
    if args.migrate_frozen and not args.append_missing:
        parser.error("--migrate-frozen requires --append-missing")

    proximity_cases = json.loads(PROXIMITY_CASES.read_text())["cases"]
    variant_cases = json.loads(VARIANT_CASES.read_text())["cases"]

    runner = bvf.Runner(args.base_url, args.interval, args.timeout)
    prior_meta = {}
    prior_retries = 0
    if args.append_missing:
        if not args.output.exists():
            parser.error("--append-missing requires an existing output file")
        frozen = json.loads(args.output.read_text())
        prior_meta = frozen["meta"]
        proximity_rows = frozen["proximity_results"]
        for row in proximity_rows:
            # Exact-anchor rank is invalid for a deliberately displaced query:
            # the construction anchor is non-nearest in 38/40 frozen cases.
            row["prox"].pop("anchor_rank", None)
        frozen_proximity_ids = {row["case_id"] for row in proximity_rows}
        current_proximity_ids = {case["id"] for case in proximity_cases}
        if frozen_proximity_ids != current_proximity_ids:
            parser.error(
                "--append-missing cannot add/remove proximity cases; run a "
                "fresh explicitly budgeted baseline"
            )
        variant_pairs = frozen["variant_results"]
        completed_ids = {pair["case_id"] for pair in variant_pairs}
        missing_cases = [case for case in variant_cases
                         if case["id"] not in completed_ids]
        stale_ids = completed_ids - {case["id"] for case in variant_cases}
        if stale_ids:
            parser.error(
                f"variant case file removed frozen ids: {sorted(stale_ids)}"
            )
        current_fingerprints = fingerprint_block(
            proximity_cases, variant_cases)
        prior_fingerprints = prior_meta.get("case_fingerprints")
        if prior_fingerprints is None:
            if not args.migrate_frozen:
                parser.error(
                    "frozen output has no case fingerprints; audit it once "
                    "with --append-missing --migrate-frozen"
                )
            if missing_cases:
                parser.error(
                    "--migrate-frozen is offline-only and requires every "
                    "current case to have a frozen response"
                )
            try:
                validate_frozen_rows(
                    proximity_cases, proximity_rows,
                    variant_cases, variant_pairs,
                )
            except ValueError as error:
                parser.error(str(error))
        else:
            if (
                prior_fingerprints.get("algorithm")
                != current_fingerprints["algorithm"]
            ):
                parser.error("unsupported frozen case-fingerprint algorithm")
            for case_id in frozen_proximity_ids:
                if (
                    prior_fingerprints["proximity"].get(case_id)
                    != current_fingerprints["proximity"][case_id]
                ):
                    parser.error(f"proximity case changed under id {case_id}")
            for case_id in completed_ids:
                if (
                    prior_fingerprints["variant"].get(case_id)
                    != current_fingerprints["variant"][case_id]
                ):
                    parser.error(f"variant case changed under id {case_id}")
            try:
                validate_frozen_rows(
                    proximity_cases, proximity_rows,
                    variant_cases, variant_pairs,
                )
            except ValueError as error:
                parser.error(str(error))
        for row in proximity_rows:
            scrub_stock_proximity_score(row)
        print(
            f"=== append variant stratum ({len(missing_cases)} new cases x 2) ==="
        )
        for case in missing_cases:
            variant_pairs.append(execute_variant_pair(runner, case))
        prior_retries = prior_meta.get("transient_retries_used", 0)
    else:
        print(f"=== proximity stratum ({len(proximity_cases)} cases) ===")
        proximity_rows = []
        for case in proximity_cases:
            result = runner.execute(case)
            result["prox"] = score_proximity(case, result)
            result["proximity"] = case["proximity"]
            scrub_stock_proximity_score(result)
            proximity_rows.append(result)

        print(f"\n=== variant stratum ({len(variant_cases)} cases x 2) ===")
        variant_pairs = [execute_variant_pair(runner, case)
                         for case in variant_cases]

    proximity_summary = summarize_proximity(proximity_rows)
    variant_summary = summarize_variants(variant_pairs)
    prior_extension_sha = prior_meta.get(
        "extension_run_base_git_sha", prior_meta.get("git_sha"))
    if prior_extension_sha == EXTENSION_RUN_BASE_GIT_SHA[:7]:
        prior_extension_sha = EXTENSION_RUN_BASE_GIT_SHA
    total_requests = len(proximity_rows) + 2 * len(variant_pairs)
    run_timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    request_provenance = request_count_provenance(
        args.append_missing,
        prior_meta,
        len(runner.results),
        total_requests,
        run_timestamp,
    )

    payload = {
        "schema": "proximity-variant-baseline-v1",
        "meta": {
            "timestamp": run_timestamp,
            "base_url": args.base_url,
            "initial_run_git_sha": prior_meta.get(
                "initial_run_git_sha",
                INITIAL_RUN_GIT_SHA if args.append_missing else bvf.git_sha(),
            ),
            "extension_run_base_git_sha": (
                prior_extension_sha if args.append_missing else None),
            "provenance_note": (
                "The four extension requests ran from a worktree based on "
                "extension_run_base_git_sha. Exact reusable request and "
                "score inputs are bound by case_fingerprints; final probe and "
                "case-file bytes are bound by content_sha256."
                if args.append_missing else None
            ),
            "data_version": (
                runner.data_version or prior_meta.get("data_version")
            ),
            "overture_places_vintage": "2026-06-17.0",
            "overture_divisions_vintage": "2026-07-22.0",
            "interval_s": args.interval,
            "cases": [str(PROXIMITY_CASES.relative_to(REPO)),
                      str(VARIANT_CASES.relative_to(REPO))],
            "requests": total_requests,
            **request_provenance,
            "transient_retries_used": prior_retries + sum(
                row.get("transient_retries") or 0 for row in runner.results),
            "case_fingerprints": fingerprint_block(
                proximity_cases, variant_cases),
            "content_sha256": {
                "probe": file_sha256(Path(__file__)),
                "proximity_cases": file_sha256(PROXIMITY_CASES),
                "variant_cases": file_sha256(VARIANT_CASES),
            },
            "interpretation_notes": [
                "chain_name_matches is permissive; Sydney credits Woolworths "
                "Riley Street Car Park, so the proximity rate is flattering",
                "four of five apostrophe controls return zero candidates, so "
                "that class has weak discriminating power; retrieval is broken "
                "under both spellings for four of five, not five of five",
                "the Domino's (IN, 9246 km), Søstrene Grene (DE, about 355 "
                "km), and Phương Đông (VN, 1134 km) control failures are "
                "global-homonym/distance failures, not proximity evidence",
            ],
        },
        "proximity_summary": proximity_summary,
        "variant_summary": variant_summary,
        "proximity_results": proximity_rows,
        "variant_results": variant_pairs,
    }
    atomic_write_json(args.output, payload)

    print("\n=== proximity summary ===")
    print(json.dumps(proximity_summary["overall"], indent=1))
    print("per country (rank10_rate / median top1 km):")
    for country, stats in proximity_summary["by_country"].items():
        print(f"  {country}: n={stats['n']} r@1={stats['rank1_rate']} "
              f"r@10={stats['rank10_rate']} "
              f"med_top1={stats['median_top1_distance_km']} km "
              f"empty={stats['empty_responses']}")
    print("\n=== variant summary ===")
    print(json.dumps(variant_summary, indent=1))
    try:
        rel = args.output.relative_to(REPO)
    except ValueError:
        rel = args.output
    print(f"\nwrote {rel}")


if __name__ == "__main__":
    main()
