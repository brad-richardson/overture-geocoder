#!/usr/bin/env python3
"""Build the proximity/"chain near me" and variant-typing benchmark strata.

Recommendation 1 of docs/plans/2026-08-06-places-failure-mode-review.md: the
existing benchmarks carry zero proximity cases and zero spelling-variant
cases, so the two largest verified skeletons (the broken proximity lane, the
tokenizer variant holes) are invisible to every metric. This builder samples
both strata from local Overture mirrors. Place gold is drawn from the exact
vintage production serves; division gold uses the newer locally available
mirror and records that mismatch on every affected case.

Sources (no credentials, no network):
- Places:    /home/brad/dev/overture-local/2026-06-17.0/theme=places/type=place
             (the production v2 vintage, 75,642,289 records)
- Divisions: /home/brad/dev/overture-local/2026-07-22.0/theme=divisions/type=division
             (hyphenated-locality class only; the local mirror has no
             2026-06-17.0 divisions tree. The sampled localities are
             population-ranked world cities and stable across releases,
             but the vintage mismatch is recorded per case.)

Outputs (benchmark-v2-forward-cases-v1 schema plus documented extension
fields, see meta.extension in each file):
- benchmarks/proximity-chain-cases-v1.json   (~40 cases, >=8 countries)
- benchmarks/variant-typing-cases-v1.json    (~20 cases, 4 variant classes)

Proximity case construction, per (metro, chain):
- anchor = the chain instance closest to the metro box centre (deterministic,
  but it is a construction aid rather than the scoring target);
- proximity point = anchor displaced 0.5-2.0 km on a bearing derived from
  md5(anchor id) (deterministic);
- the case queries the bare chain name with that proximity, expects the
  anchor (id + coordinates), and records how many instances of the chain sit
  within 2 km of the proximity point plus the distance of the nearest one --
  the material for the nearest-k assertion ("is the top result within 2 km?").

Variant case construction, per class: pick real corpus records whose primary
name carries the variant character, query the ASCII/typed spelling users
produce, and keep the original spelling as control_query so the runner can
separate "record not retrievable at all" from "variant spelling breaks it".

Usage: python benchmarks/probes/2026-08-06-build-proximity-variant-cases.py
"""

import hashlib
import json
import re
import subprocess
import unicodedata
from datetime import datetime, timezone
from math import atan2, cos, radians, sin, sqrt
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DUCKDB = "/home/brad/.duckdb/cli/latest/duckdb"
PLACES = "/home/brad/dev/overture-local/2026-06-17.0/theme=places/type=place/*.parquet"
DIVISIONS = (
    "/home/brad/dev/overture-local/2026-07-22.0/theme=divisions/type=division/*.parquet"
)
PLACES_SOURCE = "$OVERTURE_LOCAL/2026-06-17.0/theme=places/type=place/*.parquet"
DIVISIONS_SOURCE = (
    "$OVERTURE_LOCAL/2026-07-22.0/theme=divisions/type=division/*.parquet"
)
CASES_SCHEMA = "benchmark-v2-forward-cases-v1"
PROXIMITY_OUT = REPO / "benchmarks/proximity-chain-cases-v1.json"
VARIANT_OUT = REPO / "benchmarks/variant-typing-cases-v1.json"

# metro -> (country, lon0, lon1, lat0, lat1). Diversity is the point:
# US-only testing hides regional failures.
METROS = {
    "seattle": ("US", -122.44, -122.24, 47.50, 47.72),
    "toronto": ("CA", -79.50, -79.28, 43.58, 43.75),
    "mexico_city": ("MX", -99.25, -99.05, 19.33, 19.50),
    "sao_paulo": ("BR", -46.75, -46.52, -23.65, -23.48),
    "london": ("GB", -0.25, 0.05, 51.44, 51.58),
    "paris": ("FR", 2.25, 2.45, 48.80, 48.92),
    "berlin": ("DE", 13.28, 13.52, 52.45, 52.58),
    "madrid": ("ES", -3.80, -3.60, 40.36, 40.48),
    "stockholm": ("SE", 17.95, 18.20, 59.28, 59.40),
    "warsaw": ("PL", 20.90, 21.10, 52.15, 52.30),
    "istanbul": ("TR", 28.90, 29.10, 40.98, 41.10),
    "tokyo": ("JP", 139.62, 139.92, 35.53, 35.83),
    "bangkok": ("TH", 100.42, 100.65, 13.65, 13.85),
    "singapore": ("SG", 103.75, 103.95, 1.25, 1.40),
    "sydney": ("AU", 151.10, 151.30, -33.95, -33.80),
    "johannesburg": ("ZA", 27.95, 28.15, -26.28, -26.10),
    "mumbai": ("IN", 72.80, 72.95, 18.95, 19.15),
    "jakarta": ("ID", 106.75, 106.95, -6.30, -6.12),
    "manila": ("PH", 120.95, 121.10, 14.52, 14.68),
    # variant-only boxes (non-decomposable Latin letters)
    "copenhagen": ("DK", 12.45, 12.65, 55.62, 55.72),
    "oslo": ("NO", 10.65, 10.85, 59.88, 59.96),
    "ho_chi_minh_city": ("VN", 106.62, 106.75, 10.72, 10.82),
}

# 40 (metro, chain) pairs over 19 countries. Counts verified >= ~20 instances
# per box on the 2026-06-17.0 corpus except where noted; the exact primary
# name spelling is what the corpus carries, so query == recorded name.
CHAIN_PAIRS = [
    ("seattle", "Starbucks"), ("seattle", "7-Eleven"),
    ("toronto", "Tim Hortons"), ("toronto", "Starbucks"),
    ("mexico_city", "OXXO"), ("mexico_city", "7-Eleven"),
    ("sao_paulo", "Drogasil"), ("sao_paulo", "McDonald's"),
    ("london", "Costa Coffee"), ("london", "Pret A Manger"),
    ("london", "Starbucks"),
    ("paris", "Carrefour City"), ("paris", "Monoprix"),
    ("paris", "McDonald's"),
    ("berlin", "REWE"), ("berlin", "Lidl"),
    ("madrid", "Mercadona"), ("madrid", "Burger King"),
    ("stockholm", "Pressbyrån"), ("stockholm", "7-Eleven"),
    ("warsaw", "Żabka"), ("warsaw", "Costa Coffee"),
    ("istanbul", "Şok"), ("istanbul", "Migros"),
    ("tokyo", "ローソン"), ("tokyo", "Starbucks"),
    ("bangkok", "7-Eleven"), ("bangkok", "KFC"),
    ("singapore", "7-Eleven"), ("singapore", "Subway"),
    ("sydney", "Woolworths"), ("sydney", "7-Eleven"),
    ("johannesburg", "KFC"), ("johannesburg", "Pick n Pay"),
    ("mumbai", "Subway"), ("mumbai", "Starbucks"),
    ("jakarta", "Indomaret"), ("jakarta", "KFC"),
    ("manila", "Jollibee"), ("manila", "7-Eleven"),
]

PROXIMITY_RADIUS_KM = 2.0

# Two deliberately lower-population divisions with a matching POI-name
# collision in the Places corpus. The first is a Monaco macrohood, so this
# supplements rather than pretending to be part of the locality-only top-five
# sample below.
TARGETED_HYPHEN_DIVISION_IDS = [
    "0a067856-c822-4b38-bdbd-4abbb24cacca",  # Monte-Carlo, MC
    "8248f197-ef50-4588-a32c-ccad1abe72ba",  # Opa-locka, US
]

# Non-decomposable Latin letters: NFKD does not reduce these to ASCII, so a
# query typed in ASCII cannot meet the indexed form (review doc section 4).
NON_DECOMPOSABLE = "øØłŁæÆœŒßđĐþÞ"
ASCII_FOLD = {
    "ø": "o", "Ø": "O", "ł": "l", "Ł": "L", "æ": "ae", "Æ": "Ae",
    "œ": "oe", "Œ": "Oe", "ß": "ss", "đ": "d", "Đ": "D", "þ": "th",
    "Þ": "Th",
}


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = (sin(dlat / 2) ** 2
         + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2)
    return r * 2 * atan2(sqrt(a), sqrt(1 - a))


def stable_hash(value):
    return int(hashlib.md5(
        value.encode("utf-8"), usedforsecurity=False).hexdigest(), 16)


def duckdb_json(sql):
    result = subprocess.run(
        [DUCKDB, "-json", "-c", sql], capture_output=True, text=True,
        check=True,
    )
    return json.loads(result.stdout) if result.stdout.strip() else []


def sql_quote(value):
    return "'" + value.replace("'", "''") + "'"


def metro_values_clause(names):
    rows = []
    for name in names:
        country, lon0, lon1, lat0, lat1 = METROS[name]
        rows.append(f"({sql_quote(name)}, {sql_quote(country)}, "
                    f"{lon0}, {lon1}, {lat0}, {lat1})")
    return ("metros(metro, country, lon0, lon1, lat0, lat1) AS (VALUES "
            + ", ".join(rows) + ")")


# ---------------------------------------------------------------------------
# Proximity stratum


def displace(lat, lon, bearing_deg, distance_km):
    dlat = distance_km * cos(radians(bearing_deg)) / 111.32
    # Every configured metro is far from a pole. Keep the helper safe if a
    # future case is not: longitude is undefined as cos(latitude) approaches 0.
    lon_scale = max(abs(cos(radians(lat))), 1e-6)
    dlon = (distance_km * sin(radians(bearing_deg))
            / (111.32 * lon_scale))
    return round(lat + dlat, 6), round(lon + dlon, 6)


def build_proximity_cases():
    metros = sorted({metro for metro, _ in CHAIN_PAIRS})
    names = sorted({name for _, name in CHAIN_PAIRS})
    sql = f"""
WITH {metro_values_clause(metros)},
p AS (
  SELECT id, names."primary" AS name, bbox.xmin AS lon, bbox.ymin AS lat
  FROM read_parquet('{PLACES}')
  WHERE names."primary" IN ({", ".join(sql_quote(n) for n in names)})
)
SELECT m.metro, p.id, p.name, p.lon, p.lat
FROM p JOIN metros m
  ON p.lon BETWEEN m.lon0 AND m.lon1 AND p.lat BETWEEN m.lat0 AND m.lat1
ORDER BY m.metro, p.name, p.id;
"""
    rows = duckdb_json(sql)
    grouped = {}
    for row in rows:
        grouped.setdefault((row["metro"], row["name"]), []).append(row)

    cases = []
    for metro, chain in CHAIN_PAIRS:
        instances = grouped.get((metro, chain), [])
        if len(instances) < 3:
            print(f"SKIP {metro}/{chain}: only {len(instances)} instances")
            continue
        country, lon0, lon1, lat0, lat1 = METROS[metro]
        center = ((lat0 + lat1) / 2, (lon0 + lon1) / 2)
        anchor = min(
            instances,
            key=lambda r: (haversine_km(*center, r["lat"], r["lon"]), r["id"]),
        )
        h = stable_hash(anchor["id"])
        bearing = h % 360
        offset_km = 0.5 + ((h // 360) % 1501) / 1000.0  # 0.5 .. 2.0 km
        prox_lat, prox_lon = displace(
            anchor["lat"], anchor["lon"], bearing, offset_km)
        distances = sorted(
            haversine_km(prox_lat, prox_lon, r["lat"], r["lon"])
            for r in instances
        )
        within = sum(d <= PROXIMITY_RADIUS_KM for d in distances)
        assert within >= 1, f"{metro}/{chain}: anchor fell outside 2 km"
        slug = re.sub(r"[^a-z0-9]+", "-",
                      unicodedata.normalize("NFKD", chain).encode(
                          "ascii", "ignore").decode().lower()).strip("-")
        cases.append({
            "id": f"prox:{metro}:{slug or stable_hash(chain) % 10**6}",
            "kind": "place",
            "query": chain,
            "query_style": "chain_proximity",
            "expected_name": chain,
            "alt_names": [],
            "expected_gers_id": anchor["id"].lower(),
            "expected_lat": anchor["lat"],
            "expected_lon": anchor["lon"],
            "tolerance_km": PROXIMITY_RADIUS_KM,
            "proximity": [prox_lon, prox_lat],
            "strata": {"scope": "proximity", "country": country,
                       "metro": metro, "chain": chain},
            "proximity_assert": {
                "nearest_within_km": PROXIMITY_RADIUS_KM,
                "offset_km": round(offset_km, 3),
                "chain_instances_in_metro_box": len(instances),
                "chain_instances_within_2km": within,
                "nearest_instance_km": round(distances[0], 3),
            },
        })
    return cases


# ---------------------------------------------------------------------------
# Variant stratum


def word_count(name):
    return len([t for t in re.split(r"\s+", name.strip()) if t])


def pick_per_metro(rows, metros, predicate, per_metro=1):
    """Deterministic pick: hash-ordered first qualifying row per metro."""
    picked = []
    for metro in metros:
        candidates = [r for r in rows if r["metro"] == metro and predicate(r)]
        candidates.sort(key=lambda r: stable_hash(r["id"]))
        picked.extend(candidates[:per_metro])
    return picked


def places_variant_rows(metros, where):
    sql = f"""
WITH {metro_values_clause(metros)},
p AS (
  SELECT id, names."primary" AS name, bbox.xmin AS lon, bbox.ymin AS lat
  FROM read_parquet('{PLACES}')
  WHERE {where}
)
SELECT m.metro, m.country, p.id, p.name, p.lon, p.lat
FROM p JOIN metros m
  ON p.lon BETWEEN m.lon0 AND m.lon1 AND p.lat BETWEEN m.lat0 AND m.lat1
ORDER BY m.metro, p.id;
"""
    return duckdb_json(sql)


def make_place_variant_case(row, variant_class, variant_query):
    return {
        "id": f"variant:{variant_class}:{row['id'].lower()}",
        "kind": "place",
        "query": variant_query,
        "query_style": f"variant_{variant_class}",
        "expected_name": row["name"],
        "alt_names": [],
        "expected_gers_id": row["id"].lower(),
        "expected_lat": row["lat"],
        "expected_lon": row["lon"],
        "tolerance_km": 1.0,
        "control_query": row["name"],
        "strata": {"scope": "variant", "variant_class": variant_class,
                   "country": row["country"], "metro": row["metro"]},
    }


def build_apostrophe_cases():
    metros = ["london", "sydney", "toronto", "johannesburg", "mumbai"]
    rows = places_variant_rows(
        metros,
        "names.\"primary\" LIKE '%''s%' AND length(names.\"primary\") < 40",
    )
    pattern = re.compile(r"[A-Za-z]'s(\s|$)")
    picked = pick_per_metro(
        rows, metros,
        lambda r: pattern.search(r["name"]) and 2 <= word_count(r["name"]) <= 3,
    )
    return [
        make_place_variant_case(row, "apostrophe", row["name"].replace("'", ""))
        for row in picked
    ]


def build_ampersand_cases():
    metros = ["london", "seattle", "sydney", "berlin", "singapore"]
    rows = places_variant_rows(
        metros,
        "names.\"primary\" LIKE '% & %' AND length(names.\"primary\") < 40",
    )
    picked = pick_per_metro(
        rows, metros,
        lambda r: 3 <= word_count(r["name"]) <= 4
        and re.fullmatch(r"[A-Za-z0-9&'.\- ]+", r["name"]) is not None,
    )
    return [
        make_place_variant_case(
            row, "ampersand",
            re.sub(r"\s*&\s*", " and ", row["name"]),
        )
        for row in picked
    ]


def fold_ascii(name):
    folded = "".join(ASCII_FOLD.get(char, char) for char in name)
    decomposed = unicodedata.normalize("NFKD", folded)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def build_nonascii_cases():
    metros = ["copenhagen", "oslo", "warsaw", "berlin", "ho_chi_minh_city"]
    classes = re.compile(f"[{NON_DECOMPOSABLE}]")
    rows = places_variant_rows(
        metros,
        "regexp_matches(names.\"primary\", '[øØłŁæÆœŒßđĐþÞ]') "
        "AND length(names.\"primary\") < 40",
    )
    picked = pick_per_metro(
        rows, metros,
        lambda r: classes.search(r["name"]) and 2 <= word_count(r["name"]) <= 3
        # Latin-script names only: the ASCII fold of a name whose remaining
        # characters are non-Latin would still be untypeable.
        and re.fullmatch(r"[\w&'.\- ]+", fold_ascii(r["name"]), re.ASCII),
    )
    return [
        make_place_variant_case(row, "nonascii_latin", fold_ascii(row["name"]))
        for row in picked
    ]


def make_hyphen_division_case(row, sample_band):
    return {
        "id": f"variant:hyphen_locality:{row['id'].lower()}",
        "kind": "place",
        "query": row["name"].replace("-", " "),
        "query_style": "variant_hyphen_locality",
        "expected_name": row["name"],
        "alt_names": [],
        "expected_lat": row["lat"],
        "expected_lon": row["lon"],
        "tolerance_km": 25.0,
        "expected_feature_type": "locality",
        "control_query": row["name"],
        "strata": {"scope": "variant", "variant_class": "hyphen_locality",
                   "country": row["country"], "metro": "-",
                   "sample_band": sample_band},
        "provenance_note": (
            "division sampled from the 2026-07-22.0 divisions mirror "
            "(no local 2026-06-17.0 divisions tree); "
            f"subtype {row['subtype']}; population {row['population']:,}"
        ),
    }


def build_hyphen_locality_cases():
    sql = f"""
SELECT id, names."primary" AS name, country, population, subtype,
       (bbox.xmin + bbox.xmax) / 2 AS lon, (bbox.ymin + bbox.ymax) / 2 AS lat
FROM read_parquet('{DIVISIONS}')
WHERE subtype = 'locality' AND names."primary" LIKE '%-%'
  AND population IS NOT NULL
  AND regexp_matches(names."primary", '^[A-Za-zÀ-ÿ .-]+$')
ORDER BY population DESC
LIMIT 60;
"""
    rows = duckdb_json(sql)
    cases, used_countries = [], set()
    for row in rows:
        if row["country"] in used_countries:
            continue
        used_countries.add(row["country"])
        cases.append(make_hyphen_division_case(row, "top_population"))
        if len(cases) == 5:
            break

    targeted_sql = f"""
SELECT id, names."primary" AS name, country, population, subtype,
       (bbox.xmin + bbox.xmax) / 2 AS lon, (bbox.ymin + bbox.ymax) / 2 AS lat
FROM read_parquet('{DIVISIONS}')
WHERE id IN ({", ".join(sql_quote(value) for value in TARGETED_HYPHEN_DIVISION_IDS)})
ORDER BY list_position(
  [{", ".join(sql_quote(value) for value in TARGETED_HYPHEN_DIVISION_IDS)}], id
);
"""
    targeted = duckdb_json(targeted_sql)
    assert [row["id"] for row in targeted] == TARGETED_HYPHEN_DIVISION_IDS
    cases.extend(make_hyphen_division_case(row, "targeted_lower_population")
                 for row in targeted)
    return cases


# ---------------------------------------------------------------------------


def write_case_file(path, cases, sources, extension_doc):
    payload = {
        "schema": CASES_SCHEMA,
        "meta": {
            "seed": None,
            # Per file, not per builder: the proximity stratum never reads the
            # divisions mirror, and claiming it would import a vintage
            # mismatch these cases do not carry.
            "sources": sources,
            "timestamp": datetime.now(timezone.utc).isoformat(
                timespec="seconds"),
            "builder": "benchmarks/probes/2026-08-06-build-proximity-variant-cases.py",
            "extension": extension_doc,
            "case_counts": {"place": len(cases)},
        },
        "cases": cases,
    }
    path.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n")
    print(f"wrote {len(cases)} cases to {path.relative_to(REPO)}")


def main():
    proximity = build_proximity_cases()
    write_case_file(
        PROXIMITY_OUT,
        proximity,
        sources=[PLACES_SOURCE],
        extension_doc=(
            "Extends benchmark-v2-forward-cases-v1 with proximity_assert "
            "{nearest_within_km, offset_km, chain_instances_in_metro_box, "
            "chain_instances_within_2km, nearest_instance_km}. "
            "expected_gers_id is only construction provenance: the displaced "
            "point makes the anchor non-nearest in most cases, so its stock "
            "exact-GERS score MUST NOT be tracked, compared, or asserted. The "
            "2026-08-06-proximity-variant-baseline probe is the sole scoring "
            "authority; it scores the rank of ANY record of the chain within "
            "2 km of the proximity point and the top-1 distance from the "
            "proximity point."
        ),
    )
    countries = sorted({c["strata"]["country"] for c in proximity})
    print(f"  proximity: {len(proximity)} cases, "
          f"{len(countries)} countries: {', '.join(countries)}")

    variants = (build_apostrophe_cases() + build_ampersand_cases()
                + build_nonascii_cases() + build_hyphen_locality_cases())
    write_case_file(VARIANT_OUT, variants,
                    sources=[PLACES_SOURCE, DIVISIONS_SOURCE],
                    extension_doc=(
                        "Extends benchmark-v2-forward-cases-v1 with "
                        "control_query (the corpus spelling; the main query "
                        "is the ASCII/typed variant users produce). "
                        "hyphen_locality has five population-leading, "
                        "country-distinct localities plus two deliberately "
                        "lower-population divisions with same-name POI "
                        "competition (see strata.sample_band). These cases "
                        "have no expected_gers_id: "
                        "they are scored by name+distance, and their gold "
                        "comes from the 2026-07-22.0 divisions mirror (see "
                        "provenance_note)."
                    ))
    for case in variants:
        print(f"  {case['id'][:60]:<62} q={case['query']!r} "
              f"(control {case['control_query']!r})")


if __name__ == "__main__":
    main()
