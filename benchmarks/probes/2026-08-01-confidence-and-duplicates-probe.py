#!/usr/bin/env python3
"""Two findings that invalidate part of the 2026-07-31 prominence analysis.

Operator challenge, 2026-08-01: "confidence isn't the importance, it's the
source's confidence from various signals" and "is it possible that eiffel tower
entry is actually a duplicate?" Both were right, and Part 6d of
`docs/plans/2026-07-31-search-quality-and-street-layer.md` is corrected by this
probe. See Part 6h there and
`benchmarks/2026-08-01-confidence-and-duplicates.json`.

FINDING 1 -- `confidence` is a PER-SOURCE value, mostly a flat default.
    Foursquare stamps exactly 0.7700 on 100% of its records; PinMeTo and DAC
    stamp exactly 1.0000; Microsoft floors at 0.85 and AllThePlaces at 0.80.
    Only `meta` carries a continuous distribution. So `confidence_rank` is, for
    roughly a fifth of the corpus, an upstream SOURCE IDENTIFIER quantized into
    a u8 -- not a quality measure.

    This is why Part 6d's "Tour Eiffel confidence 0.7700, rank 202 of 299"
    reads the way it does. The canonical Tour Eiffel is a FOURSQUARE record and
    every Foursquare record is 0.7700. It was never a fame measurement.

    EXCEPT IN THE US, which is a different regime entirely: Seattle and NYC are
    heavily conflated (LEN(sources) = 7 for the majority, against exactly 2 for
    100% of records in every non-US region tested), and even single-source
    Foursquare is not flat there. Any rule keyed on the flat-default pattern
    behaves differently in the US than everywhere else.

FINDING 2 -- the Eiffel Tower is ~87 records under 53 distinct name-forms.
    The head index keeps 10 entries per token. `q=Eiffel Tower` is not losing
    to hotels; it is losing to ITSELF, scattered up to 17.8 km from the true
    coordinates, in Latin typos, transliterations and CJK/Arabic/Thai scripts.

    A simple dedup heuristic does NOT reach this. Exact normalised-name
    equality at UNLIMITED radius still leaves 53 name-forms. Fuzzy matching
    gets to 17 but is unshippable: no Jaro-Winkler threshold exists, because
    the score ordering is inverted (Statue of Liberty / Statue of Liberty Deli
    scores 0.958 and must NOT merge; Colosseo / Coliseo Romano scores 0.830 and
    MUST). See the `jaro_winkler_inversion` block in the evidence JSON.

LANDMINE, recorded regardless of whether dedup is ever built:
    the obvious normaliser `regexp_replace(lower(strip_accents(n)),'[^a-z]','','g')`
    collapses 292,285 of 420,726 Tokyo records (69.47%) to the EMPTY STRING.
    Paris is 0.21% and NYC 0.23%, so this is invisible in Latin-script testing
    and catastrophic in CJK. Use `[^\\p{L}\\p{N}]`, which RE2 supports.

Run:  uv run --with 'duckdb>=1.5' --with pandas python3 <this file>
Reads Overture directly from S3; needs no credentials and writes nothing.
"""
import duckdb

REL = "2026-06-17.0"
SRC = f"s3://overturemaps-us-west-2/release/{REL}/theme=places/type=place/*"

# Eiffel Tower true coordinates.
TOWER_LON, TOWER_LAT = 2.29448, 48.85837

# Kept deliberately small and bbox-bounded: every claim below is reproducible
# in minutes, and no query scans the planet.
REGIONS = {
    "paris_fr":     (2.20, 48.80, 2.40, 48.92),
    "berlin_de":    (13.30, 52.45, 13.50, 52.57),
    "tokyo_jp":     (139.65, 35.63, 139.85, 35.75),
    "mumbai_in":    (72.80, 18.90, 73.00, 19.15),
    "lagos_ng":     (3.30, 6.40, 3.50, 6.60),
    "sao_paulo_br": (-46.75, -23.65, -46.55, -23.50),
    # The US control. Included because it BREAKS the pattern, not because it
    # confirms it -- omitting it would have produced a false "global" claim.
    "seattle_us":   (-122.45, 47.50, -122.20, 47.72),
}

ADMITTED = ("names.primary IS NOT NULL AND "
            "COALESCE(operating_status,'open') != 'permanently_closed'")

con = duckdb.connect()
con.execute("INSTALL httpfs; LOAD httpfs; INSTALL spatial; LOAD spatial; "
            "SET s3_region='us-west-2';")
con.execute("SET memory_limit='6GB'; SET threads=4;")

print(f"duckdb {duckdb.__version__}   overture {REL}\n")


def bbox(region):
    x0, y0, x1, y1 = REGIONS[region]
    return (f"bbox.xmin BETWEEN {x0} AND {x1} AND "
            f"bbox.ymin BETWEEN {y0} AND {y1}")


# ---------------------------------------------------------------- FINDING 1
print("=" * 78)
print("FINDING 1a. confidence by source dataset -- flat defaults per source")
print("=" * 78)
for region in REGIONS:
    print(f"\n--- {region} ---")
    print(con.execute(f"""
        SELECT sources[1].dataset AS dataset,
               COUNT(*) AS n,
               COUNT(DISTINCT confidence) AS distinct_conf,
               ROUND(MIN(confidence),4) AS lo,
               ROUND(MEDIAN(confidence),4) AS p50,
               ROUND(MAX(confidence),4) AS hi
        FROM read_parquet(?, hive_partitioning=true)
        WHERE {bbox(region)} AND {ADMITTED}
        GROUP BY 1 ORDER BY n DESC
    """, [SRC]).df().to_string(index=False))

print()
print("=" * 78)
print("FINDING 1b. source-array width -- the US is conflated, elsewhere is not")
print("=" * 78)
for region in REGIONS:
    row = con.execute(f"""
        SELECT COUNT(*) AS n,
               ROUND(100.0*SUM(CASE WHEN LEN(sources)=2 THEN 1 ELSE 0 END)
                     /COUNT(*),2) AS pct_exactly_two_sources,
               MAX(LEN(sources)) AS max_sources
        FROM read_parquet(?, hive_partitioning=true)
        WHERE {bbox(region)} AND {ADMITTED}
    """, [SRC]).fetchone()
    print(f"  {region:<14} n={row[0]:>7}  two_sources={row[1]:>6}%  max={row[2]}")

# ---------------------------------------------------------------- FINDING 2
print()
print("=" * 78)
print("FINDING 2. Eiffel Tower duplicate census, by normalised name-form")
print("=" * 78)
# `[^a-z]` deliberately NOT used here -- see the LANDMINE note in the docstring.
NORM = r"regexp_replace(lower(strip_accents(names.primary)), '[^\p{L}\p{N}]', '', 'g')"
print(con.execute(f"""
    SELECT {NORM} AS normalised, COUNT(*) AS n,
           ROUND(MIN(confidence),3) AS conf_lo,
           ROUND(MAX(confidence),3) AS conf_hi,
           ROUND(MIN(ST_Distance_Sphere(geometry, ST_Point(?,?))),0) AS nearest_m,
           ROUND(MAX(ST_Distance_Sphere(geometry, ST_Point(?,?))),0) AS farthest_m
    FROM read_parquet(?, hive_partitioning=true)
    WHERE {bbox('paris_fr')} AND {ADMITTED}
      AND (COALESCE(categories.primary,'') IN
            ('monument','landmark_and_historical_building','tourist_attraction')
           OR list_contains(COALESCE(categories.alternate, []), 'monument')
           OR list_contains(COALESCE(categories.alternate, []),
                            'landmark_and_historical_building'))
      AND {NORM} SIMILAR TO '.*(eiffel|eifel|effeil|eiffell).*'
    GROUP BY 1 ORDER BY n DESC, nearest_m
""", [TOWER_LON, TOWER_LAT, TOWER_LON, TOWER_LAT, SRC]).df().to_string(index=False))

# ---------------------------------------------------------------- LANDMINE
print()
print("=" * 78)
print("LANDMINE. `[^a-z]` normalisation destroys CJK; `[^\\p{L}\\p{N}]` does not")
print("=" * 78)
for region in ("tokyo_jp", "paris_fr", "seattle_us"):
    row = con.execute(f"""
        SELECT COUNT(*) AS n,
               ROUND(100.0*SUM(CASE WHEN regexp_replace(
                     lower(strip_accents(names.primary)),'[^a-z]','','g')=''
                 THEN 1 ELSE 0 END)/COUNT(*),2) AS pct_empty_ascii_only,
               ROUND(100.0*SUM(CASE WHEN {NORM}='' THEN 1 ELSE 0 END)
                     /COUNT(*),4) AS pct_empty_unicode_aware
        FROM read_parquet(?, hive_partitioning=true)
        WHERE {bbox(region)} AND {ADMITTED}
    """, [SRC]).fetchone()
    print(f"  {region:<14} n={row[0]:>7}  [^a-z] empties {row[1]:>6}%   "
          f"[^\\p{{L}}\\p{{N}}] empties {row[2]:>7}%")
