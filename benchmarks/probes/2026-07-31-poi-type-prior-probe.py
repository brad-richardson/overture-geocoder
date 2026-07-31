#!/usr/bin/env python3
"""Revised hypothesis: the ONLY in-data prominence signal for Overture places
is the CATEGORY, not any count or confidence column.

Basilica de la Sagrada Familia: confidence 0.9897, websites 1, socials 1,
phones 1, sources 2, common_names 0 -- identical to the Starbucks next door,
and LOWER confidence than the vet clinic.
Its one distinguishing field: categories.alternate =
  ['landmark_and_historical_building', 'monument'].

Test: does a static category type-prior admit the landmark into the 10-entry
head cap that confidence-DESC evicts it from?
"""
import duckdb, math

REL = "2026-06-17.0"
SRC = f"s3://overturemaps-us-west-2/release/{REL}/theme=places/type=place/*"
con = duckdb.connect()
con.execute("INSTALL httpfs; LOAD httpfs; INSTALL spatial; LOAD spatial; "
            "SET s3_region='us-west-2';")
con.execute("SET memory_limit='6GB'; SET threads=4;")

# Static type prior, same shape build_shards.py already uses for divisions.
# Keyed on the union of categories.primary + categories.alternate + basic.
LANDMARK = {
    "landmark_and_historical_building": 1.00,
    "monument": 1.00,
    "tourist_attraction": 0.95,
    "museum": 0.85,
    "castle": 0.85,
    "palace": 0.85,
    "cathedral": 0.80,
    "catholic_church": 0.60,
    "christian_place_of_worship": 0.55,
    "place_of_worship": 0.50,
    "park": 0.45,
    "stadium_arena": 0.45,
    "airport": 0.90,
    "train_station": 0.55,
    "university": 0.55,
    "zoo": 0.60,
    "aquarium": 0.60,
    "art_gallery": 0.50,
    "theatre": 0.45,
}
# Explicitly commodity: chains and services that saturate confidence.
COMMODITY = {
    "coffee_shop", "hotel", "restaurant", "cafe", "veterinarian", "bar",
    "motel", "accommodation", "fast_food_restaurant", "convenience_store",
    "grocery_store", "pharmacy", "bank", "atm", "gas_station", "hair_salon",
    "real_estate_agent", "insurance_agency", "dentist", "gym", "laundry",
}

SQL = """
SELECT names.primary AS name, confidence,
       categories.primary AS cat, categories.alternate AS alt,
       basic_category AS basic, taxonomy.primary AS taxo
FROM read_parquet(?, hive_partitioning=true)
WHERE bbox.xmin BETWEEN ? AND ? AND bbox.ymin BETWEEN ? AND ?
  AND names.primary IS NOT NULL
  AND COALESCE(operating_status,'open') != 'permanently_closed'
  AND lower(strip_accents(names.primary)) LIKE ?
"""

BOXES = {
    "barcelona/sagrada": (2.10, 41.35, 2.25, 41.45, "%sagrada%",
                          "Basílica de la Sagrada Família"),
    "paris/eiffel":      (2.20, 48.80, 2.40, 48.92, "%eiffel%", "Tour Eiffel"),
    "seattle/seattle":   (-122.45, 47.50, -122.20, 47.72, "%seattle%", None),
}


# `landmark_and_historical_building` is NOISY: Overture tags US apartment
# buildings with it. `monument` / `tourist_attraction` are clean. Weight
# accordingly, and let the PRIMARY category dominate the alternates -- a
# holiday rental that lists `monument` as an alternate is not a monument.
LANDMARK["landmark_and_historical_building"] = 0.35

def type_prior(r):
    primary = {v for v in (r["cat"], r["basic"], r["taxo"]) if v}
    alt = set(r["alt"] or [])
    if primary & COMMODITY:
        return 0.0                      # primary category is dispositive
    p = max((LANDMARK.get(t, 0.0) for t in primary), default=0.0)
    a = max((LANDMARK.get(t, 0.0) for t in alt), default=0.0)
    return max(p, 0.5 * a)              # alternates are a weaker booster


for label, (x0, y0, x1, y1, pat, target) in BOXES.items():
    cur = con.execute(SQL, [SRC, x0, x1, y0, y1, pat])
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, t)) for t in cur.fetchall()]
    for r in rows:
        r["tp"] = type_prior(r)
    by_conf = sorted(rows, key=lambda r: (-r["confidence"], r["name"]))
    # proposed eviction key: type prior first, confidence only as tie-break
    by_tp = sorted(rows, key=lambda r: (-r["tp"], -r["confidence"], r["name"]))

    print(f"\n{'='*94}\n### {label}  n={len(rows)}\n{'='*94}")
    print("\n-- PROPOSED eviction (type_prior DESC, confidence DESC) -- top 10")
    for i, r in enumerate(by_tp[:10], 1):
        print(f"  {i:>2}. {str(r['name'])[:48]:<50} tp={r['tp']:.2f} "
              f"conf={r['confidence']:.3f} cat={str(r['cat'])[:24]}")
    if target:
        hit = [r for r in rows if r["name"] == target]
        if not hit:
            print(f"\n  !! target {target!r} NOT IN SLICE")
            continue
        t = hit[0]
        ic, it = by_conf.index(t) + 1, by_tp.index(t) + 1
        print(f"\n  TARGET {target!r}  tp={t['tp']:.2f} conf={t['confidence']:.4f}")
        print(f"    rank by confidence (CURRENT) : {ic:>4} / {len(rows)}"
              f"   {'IN CAP' if ic <= 10 else 'EVICTED'}")
        print(f"    rank by type prior (PROPOSED): {it:>4} / {len(rows)}"
              f"   {'IN CAP' if it <= 10 else 'EVICTED'}")
