#!/usr/bin/env python3
"""Measure WHY an indexed place loses the global head cap.

The interrogation (docs/plans/2026-08-04-head-miss-interrogation.md) splits
benchmark misses into ABSENT / NOT_ADMITTED / IN_HEAD.  IN_HEAD is the class
where the corpus has the record AND the producer emitted a head candidate for
it, so the loss happens later -- at the merge, which keeps only
``HEAD_RESULT_CAP`` rows per token.

That verdict does not say how badly the record lost, and the difference
matters:

  * rank 11 of 40   -> a cap raise recovers it.
  * rank 58 of 452  -> a cap raise recovers it only at an absurd cap, and the
                       real question is what ORDERS those 452.

So this probe computes, for each IN_HEAD miss, the record's exact rank under
``CAP_ORDER`` within every token its query would look up, straight from the
head-candidate packs.  Those packs carry every column of the ranking key, so
this is a measurement of the emitted data rather than a reimplementation of
the producer.

It also reports what DECIDES the top of each token.  ``prominence_rank`` is a
per-category prior, and ``COMMODITY_CATEGORIES`` maps a whole class of
everyday POIs (hotels, restaurants, shops) to 0.  Where every contender for a
token is prominence 0 and confidence is a flat per-source constant, the cap
key falls through to ``feature_id`` -- UUID order.  A cap decided by UUID
order cannot be fixed by raising the cap, and the probe is built to make that
visible rather than to assume it.

Usage:

    2026-08-04-head-cap-eviction-rank-probe.py \\
        --interrogation benchmarks/...-head-miss-interrogation-v1.json \\
        --head-candidates 'PATH/head-candidates/sha256/*' \\
        --output benchmarks/2026-08-04-head-cap-eviction-ranks.json

Limitations, stated up front:

  * It ranks against head CANDIDATES, which is what the merge consumes.  The
    merge is a pure top-n over exactly this key, so the rank is the rank; but
    the probe does not read the merged shards.
  * Query tokenization mirrors the producer's word normalization.  Queries
    above ``HEAD_QUERY_TOKEN_CAP`` words are reported as REFUSED_BY_TOKEN_CAP
    rather than ranked -- the head lane declines them outright, so a rank
    would be a fiction.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import time
import unicodedata
from pathlib import Path

# Frozen worker constants.  Both are contract-bound; see
# crates/geocoder-worker/src/places_construction_v1.rs.
HEAD_RESULT_CAP = 10
HEAD_QUERY_TOKEN_CAP = 3

SCHEMA = "overture-places-head-cap-eviction-rank-v1"
ROOT = Path(__file__).resolve().parents[2]

# The cap key, as a single lexicographically-comparable hex string.  Segment
# widths are fixed, so ordering the concatenation orders the tuple.  Each
# segment is inverted where CAP_ORDER sorts DESC.
#
#   IDENTIFYING_FIRST      ((field_mask & 3) != 0) DESC
#   prominence_rank DESC
#   confidence_rank DESC
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


def normalized_words(value: str) -> list[str]:
    """The producer's word normalization: NFKD, lower, strip marks, split."""
    folded = unicodedata.normalize("NFKD", value).lower()
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    folded = re.sub(r"[^\w]+", " ", folded, flags=re.UNICODE)
    return [word for word in folded.split() if word]


def probe_degrees(tolerance_km: float) -> float:
    return max(tolerance_km, 1.0) * 2.0 / 111.0


def load_cases(path: Path) -> tuple[list[dict], dict]:
    payload = json.loads(path.read_text())
    verdicts = payload["verdicts"]
    # The interrogation records the case-file path but not the expected point,
    # and the point is what identifies the target among same-named records.
    case_path = Path(payload["cases"])
    if not case_path.is_absolute():
        case_path = ROOT / case_path
    source = json.loads(case_path.read_text())
    points = {
        item["id"]: (item.get("expected_lat"), item.get("expected_lon"))
        for item in (source["cases"] if isinstance(source, dict) else source)
    }
    cases = []
    for verdict in verdicts:
        lat, lon = points.get(verdict["case_id"], (None, None))
        verdict = {**verdict, "expected_lat": lat, "expected_lon": lon}
        if verdict.get("hit"):
            continue
        if verdict.get("verdict") != "IN_HEAD":
            continue
        # Only EXACT matches identify the target record. The interrogation's
        # CONTAINMENT_ONLY / ALT_NAME tiers match things like "Fushimi Times
        # Square" for the query "Times Square" -- useful for asking whether
        # ANYTHING is indexed near the point, useless for asking where a
        # specific record ranks. Ranking those would rank the wrong record.
        if verdict.get("match_strength") != "EXACT":
            continue
        if verdict.get("expected_feature_type") == "locality":
            continue
        cases.append(verdict)
    return cases, payload


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interrogation", type=Path, required=True)
    parser.add_argument("--head-candidates", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--memory-limit", default="8GB")
    parser.add_argument("--threads", type=int, default=4)
    args = parser.parse_args(argv)

    import duckdb

    cases, source_payload = load_cases(args.interrogation)
    print(f"{len(cases)} IN_HEAD misses to rank", flush=True)

    # Split the cases the head lane declines outright from the ones it ranks.
    rankable: list[dict] = []
    refused: list[dict] = []
    for case in cases:
        words = normalized_words(case["query"])
        if len(words) > HEAD_QUERY_TOKEN_CAP:
            refused.append({**case, "query_words": words})
        else:
            rankable.append({**case, "query_words": words})
    print(f"  {len(rankable)} rankable, {len(refused)} refused by token cap",
          flush=True)

    con = duckdb.connect()
    con.execute(f"SET memory_limit='{args.memory_limit}'")
    con.execute(f"SET threads={args.threads}")
    con.execute(f"SET temp_directory='{args.output.parent}/.duckdb-tmp'")

    tokens = sorted({word for case in rankable for word in case["query_words"]})
    if not tokens:
        print("no tokens to rank", flush=True)
        return 1
    print(f"  {len(tokens)} distinct query tokens", flush=True)

    # The saturation ceiling. Candidate packs are capped per map task, so a
    # token that reaches tasks * HEAD_RESULT_CAP rows lost rows to that cap in
    # every task -- and a target absent from a SATURATED token was evicted,
    # while a target absent from an unsaturated one was never indexed under it.
    # Distinguishing those two is the whole point of this probe.
    pack_count = con.execute(
        f"SELECT count(DISTINCT filename) FROM read_parquet("
        f"'{args.head_candidates}', filename=true)").fetchone()[0]
    saturation_ceiling = pack_count * HEAD_RESULT_CAP
    print(f"  {pack_count} candidate packs -> saturation ceiling "
          f"{saturation_ceiling:,} rows/token", flush=True)

    con.execute("CREATE TABLE query_token(token VARCHAR)")
    con.executemany("INSERT INTO query_token VALUES (?)", [(t,) for t in tokens])

    # One filtered scan of the candidate packs.  Only rows whose token is
    # actually queried survive, which is what keeps this affordable.
    started = time.monotonic()
    print("scanning head candidates ...", flush=True)
    con.execute(f"""
        CREATE TABLE contender AS
        SELECT h.token, h.feature_id, h.filename, h.primary_name, h.category,
               h.longitude, h.latitude,
               h.prominence_rank, h.confidence_rank, h.field_mask,
               ({CAP_KEY_SQL}) AS cap_key
        FROM read_parquet('{args.head_candidates}', filename=true) h
        JOIN query_token q ON q.token = h.token
    """)
    scanned = con.execute("SELECT count(*) FROM contender").fetchone()[0]
    print(f"  {scanned:,} contender rows in {time.monotonic() - started:.1f}s",
          flush=True)

    # Per-token contention and what decides its top slice.
    token_stats = {}
    for row in con.execute(f"""
        SELECT token,
               count(*) AS rows_total,
               count(*) FILTER (WHERE prominence_rank > 0) AS rows_prominent,
               count(*) FILTER (WHERE (field_mask & 3) != 0) AS rows_identifying
        FROM contender GROUP BY token
    """).fetchall():
        token_stats[row[0]] = {
            "rows_total": row[1],
            "rows_prominent": row[2],
            "rows_identifying": row[3],
        }

    # What survives the cap today, per token, and on what discriminator.
    for token in tokens:
        top = con.execute("""
            SELECT prominence_rank, confidence_rank, field_mask
            FROM contender WHERE token = ?
            ORDER BY cap_key LIMIT ?
        """, [token, HEAD_RESULT_CAP]).fetchall()
        stats = token_stats.setdefault(token, {})
        stats["retained_prominence"] = sorted({r[0] for r in top})
        stats["retained_confidence"] = sorted({r[1] for r in top})
        # If every retained row ties on prominence AND confidence AND
        # identifying, the surviving ten were chosen by UUID order.
        stats["cap_decided_by_feature_id"] = (
            len(top) == HEAD_RESULT_CAP
            and len({(r[0], r[1], (r[2] & 3) != 0) for r in top}) == 1
        )

    # Resolve each target's feature_id from ANY token it appears under. A
    # record absent from a queried token is still present elsewhere in the
    # packs -- under its rarer name words -- and that is what proves it was
    # processed and emitted rather than never indexed at all.
    print("resolving target identities ...", flush=True)
    con.execute("""
        CREATE TABLE target_probe(
            case_id VARCHAR, name VARCHAR,
            lat_lo DOUBLE, lat_hi DOUBLE, lon_lo DOUBLE, lon_hi DOUBLE)
    """)
    probe_rows = []
    for case in rankable:
        degrees = probe_degrees(case.get("tolerance_km") or 1.0)
        lat = case.get("expected_lat")
        lon = case.get("expected_lon")
        names = {n.lower() for n in (case.get("head_name_matches") or [])}
        names.add((case.get("expected_name") or "").lower())
        for name in sorted(n for n in names if n):
            probe_rows.append((
                case["case_id"], name,
                (lat - degrees) if lat is not None else -90.0,
                (lat + degrees) if lat is not None else 90.0,
                (lon - degrees) if lon is not None else -180.0,
                (lon + degrees) if lon is not None else 180.0))
    con.executemany("INSERT INTO target_probe VALUES (?,?,?,?,?,?)", probe_rows)
    started = time.monotonic()
    con.execute(f"""
        CREATE TABLE target_identity AS
        SELECT case_id, feature_id, any_value(primary_name) AS primary_name,
               any_value(category) AS category,
               any_value(filename) AS filename,
               any_value(brand_name) AS brand_name,
               any_value(locality) AS locality,
               any_value(region) AS region,
               any_value(country) AS country,
               max(prominence_rank) AS prominence_rank,
               max(confidence_rank) AS confidence_rank,
               count(*) AS candidate_rows
        FROM (
            SELECT t.case_id, h.feature_id, h.filename, h.primary_name,
                   h.category, h.prominence_rank, h.confidence_rank,
                   h.brand_name, h.locality, h.region, h.country
            FROM read_parquet('{args.head_candidates}', filename=true) h
            JOIN target_probe t
              ON lower(h.primary_name) = t.name
             AND h.latitude BETWEEN t.lat_lo AND t.lat_hi
             AND h.longitude BETWEEN t.lon_lo AND t.lon_hi
        )
        GROUP BY case_id, feature_id
    """)
    identity = {}
    for row in con.execute("""
        SELECT case_id, feature_id, primary_name, category, prominence_rank,
               confidence_rank, candidate_rows, filename,
               brand_name, locality, region, country
        FROM target_identity
        QUALIFY row_number() OVER (
            PARTITION BY case_id ORDER BY candidate_rows DESC, feature_id) = 1
    """).fetchall():
        identity[row[0]] = {
            "feature_id": row[1], "feature_uuid": bytes(row[1]).hex(),
            "primary_name": row[2], "category": row[3],
            "prominence_rank": row[4], "confidence_rank": row[5],
            "tokens_emitted": row[6], "pack": row[7],
            # The record's own indexed fields. A token the producer would emit
            # for this record is one of THESE words; anything else it cannot
            # have emitted, however full the pack was.
            "emittable_tokens": sorted({
                word
                for field in (row[2], row[8], row[3], row[9], row[10], row[11])
                for word in normalized_words(field or "")}),
        }
    print(f"  resolved {len(identity)}/{len(rankable)} targets in "
          f"{time.monotonic() - started:.1f}s", flush=True)

    results = []
    for case in rankable:
        degrees = probe_degrees(case.get("tolerance_km") or 1.0)
        lat = case.get("expected_lat")
        lon = case.get("expected_lon")
        expected = case.get("expected_name") or ""
        per_token = []
        target_identity = identity.get(case["case_id"])
        for token in case["query_words"]:
            stats = token_stats.get(token, {})
            saturated = stats.get("rows_total", 0) >= saturation_ceiling
            if target_identity is None:
                per_token.append({
                    "token": token, "rank": None,
                    "reason": "UNRESOLVED_TARGET",
                    "detail": "no candidate row anywhere matches this case's "
                              "name and expected point",
                })
                continue
            target = con.execute(
                "SELECT cap_key, primary_name, category, prominence_rank,"
                " confidence_rank, field_mask FROM contender"
                " WHERE token = ? AND feature_id = ?"
                " ORDER BY cap_key LIMIT 1",
                [token, target_identity["feature_id"]]).fetchone()
            if target is None:
                # The record IS in the packs under other tokens, so its map
                # task ran and emitted it. Whether this token was dropped or
                # never produced is decided in THAT task's pack, not globally:
                # if the pack emitted a full HEAD_RESULT_CAP rows for the
                # token, the task's top-n was binding and the record lost it.
                # Anything short of a full cap means there was room and the
                # producer simply never emitted the token for this record.
                in_pack = con.execute(
                    "SELECT count(*) FROM contender"
                    " WHERE token = ? AND filename = ?",
                    [token, target_identity["pack"]]).fetchone()[0]
                # A full pack alone cannot separate "evicted" from "never
                # emitted" -- absence is explained either way. The record's own
                # fields settle it: the producer emits a token only if one of
                # them contains that word.
                emittable = token in target_identity["emittable_tokens"]
                pack_full = in_pack >= HEAD_RESULT_CAP
                if not pack_full:
                    # Room to spare, so nothing was dropped. Sound regardless
                    # of what the display columns show.
                    reason = "TOKEN_NOT_EMITTED"
                elif emittable:
                    reason = "EVICTED_BEFORE_MERGE"
                else:
                    # The pack is full AND the display columns do not carry the
                    # word -- but they do not carry COMMON names either, and
                    # the producer indexes those under the same identifying
                    # field mask. Absence is explained by eviction or by
                    # non-emission and the packs cannot say which.
                    reason = "INDETERMINATE_PACK_FULL"
                evicted = reason == "EVICTED_BEFORE_MERGE"
                per_token.append({
                    "token": token, "rank": None,
                    "reason": reason,
                    "contenders": stats.get("rows_total"),
                    "rows_in_targets_own_pack": in_pack,
                    "token_saturated_globally": saturated,
                    "token_is_emittable_for_target": emittable,
                    "detail": (
                        "the record carries this word in an indexed field and "
                        "its own map task filled the per-task cap for the "
                        "token, so it was dropped before the merge"
                        if evicted else
                        "the record carries this word in no indexed field, so "
                        "the producer never emits the token for it"
                        if not emittable else
                        "the target's own map task had cap room to spare, so "
                        "the producer emits no candidate for this record "
                        "under this token"),
                })
                continue
            rank = con.execute(
                "SELECT count(*) + 1 FROM contender"
                " WHERE token = ? AND cap_key < ?",
                [token, target[0]]).fetchone()[0]
            per_token.append({
                "token": token,
                "rank": rank,
                "contenders": token_stats[token]["rows_total"],
                "survives_current_cap": rank <= HEAD_RESULT_CAP,
                "target_prominence_rank": target[3],
                "target_confidence_rank": target[4],
                "target_identifying": (target[5] & 3) != 0,
                "target_category": target[2],
                "cap_decided_by_feature_id":
                    token_stats[token]["cap_decided_by_feature_id"],
            })

        ranked = [t for t in per_token if t.get("rank") is not None]
        missing_token = [t for t in per_token if t.get("rank") is None]
        reasons = sorted({t["reason"] for t in missing_token if "reason" in t})
        # The head intersects per-token top-n lists, so the record must
        # survive in EVERY queried token.  The binding cap is the worst rank.
        cap_needed = max((t["rank"] for t in ranked), default=None)
        results.append({
            "case_id": case["case_id"],
            "query": case["query"],
            "expected_name": expected,
            "strata": case.get("strata", {}),
            "query_words": case["query_words"],
            "tokens": per_token,
            "target": None if target_identity is None else {
                key: value for key, value in target_identity.items()
                if key != "feature_id"},
            "cap_needed_to_recover": None if missing_token else cap_needed,
            "recoverable_by_cap_raise": (
                not missing_token and cap_needed is not None
            ),
            "blocked_by": reasons,
            # A record evicted before the merge lost a cap, so a BIG ENOUGH cap
            # would recover it -- but the packs cannot say how big, because the
            # rows that beat it were discarded. Kept separate from the ranks
            # this probe can actually state.
            "cap_bound_unknown": "EVICTED_BEFORE_MERGE" in reasons,
        })

    recoverable = [r for r in results if r["recoverable_by_cap_raise"]]
    by_cap = {}
    for threshold in (10, 20, 50, 100, 250, 500, 1000):
        by_cap[threshold] = sum(
            1 for r in recoverable if r["cap_needed_to_recover"] <= threshold)

    uuid_decided = sum(
        1 for r in results
        for t in r["tokens"]
        if t.get("cap_decided_by_feature_id"))
    uuid_decided_cases = sum(
        1 for r in results
        if any(t.get("cap_decided_by_feature_id") for t in r["tokens"]))
    blocked = {}
    for result in results:
        for reason in result["blocked_by"]:
            blocked[reason] = blocked.get(reason, 0) + 1

    evidence = {
        "schema": SCHEMA,
        "source_interrogation": str(args.interrogation),
        "source_interrogation_schema": source_payload.get("schema"),
        "head_result_cap": HEAD_RESULT_CAP,
        "head_query_token_cap": HEAD_QUERY_TOKEN_CAP,
        "cap_order": (
            "((field_mask & 3) != 0) DESC, prominence_rank DESC, "
            "confidence_rank DESC, feature_id, source_object_index, "
            "source_row_group, source_row_index"),
        "counts": {
            "in_head_misses": len(cases),
            "rankable": len(rankable),
            "refused_by_token_cap": len(refused),
            "recoverable_by_cap_raise": len(recoverable),
            "blocked_by_reason": blocked,
            "saturation_ceiling_rows_per_token": saturation_ceiling,
            "candidate_packs": pack_count,
            "cases_with_a_uuid_decided_token": uuid_decided_cases,
            "tokens_decided_by_feature_id": uuid_decided,
        },
        "recovered_at_cap": by_cap,
        "refused_cases": [
            {"case_id": c["case_id"], "query": c["query"],
             "query_words": c["query_words"]} for c in refused],
        "token_stats": token_stats,
        "cases": results,
        "limitations": [
            "Ranks head CANDIDATES, the merge's input, not the merged shards.",
            "Candidate packs are ALREADY capped per map task, so a token's rows "
            "top out at tasks * HEAD_RESULT_CAP. A computed rank <= "
            "HEAD_RESULT_CAP is exact (that is the decomposability property the "
            "cap key is built on); a computed rank ABOVE it is a LOWER BOUND, "
            "because better-ranked siblings may have been dropped by their own "
            "task's cap. Read 'cap_needed_to_recover' above 10 as 'at least "
            "this much', never as a sufficient cap.",
            "Query tokenization mirrors the producer's word normalization; it "
            "is not the Worker's tokenizer executed in-process.",
            "A cap raise is scored as recovery only if the record survives in "
            "EVERY queried token, since the head intersects per-token lists.",
        ],
    }
    args.output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    print(json.dumps(evidence["counts"], indent=2, sort_keys=True))
    print(json.dumps({"recovered_at_cap": by_cap}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
