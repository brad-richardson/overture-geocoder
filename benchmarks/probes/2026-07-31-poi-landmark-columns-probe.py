#!/usr/bin/env python3
"""What, if anything, in Overture places distinguishes a world landmark
from a vet clinic on the same token? Dump every candidate column."""
import duckdb, json

REL = "2026-06-17.0"
SRC = f"s3://overturemaps-us-west-2/release/{REL}/theme=places/type=place/*"
con = duckdb.connect()
con.execute("INSTALL httpfs; LOAD httpfs; INSTALL spatial; LOAD spatial; SET s3_region='us-west-2';")
con.execute("SET memory_limit='6GB'; SET threads=4;")

SQL = """
SELECT names.primary AS name,
       confidence,
       COALESCE(cardinality(names.common), 0) AS common_names,
       names.rules IS NOT NULL                AS has_rules,
       categories.primary                     AS category,
       categories.alternate                   AS alt_categories,
       basic_category,
       taxonomy.primary                       AS taxonomy_primary,
       brand.wikidata                         AS brand_wikidata,
       COALESCE(LEN(websites),0)  AS websites,
       COALESCE(LEN(socials),0)   AS socials,
       COALESCE(LEN(phones),0)    AS phones,
       LEN(sources)               AS all_sources,
       list_transform(sources, lambda s: s.dataset) AS datasets,
       ST_X(geometry) AS lon, ST_Y(geometry) AS lat
FROM read_parquet(?, hive_partitioning=true)
WHERE bbox.xmin BETWEEN ? AND ? AND bbox.ymin BETWEEN ? AND ?
  AND names.primary IS NOT NULL
  AND lower(strip_accents(names.primary)) LIKE ?
ORDER BY confidence DESC
"""

PROBES = [
    ("BASILICA",  2.16, 41.39, 2.19, 41.42, "%basilica%sagrada%"),
    ("EIFFEL TWR",2.28, 48.85, 2.30, 48.87, "%tour eiffel%"),
    ("VET CLINIC",2.10, 41.35, 2.25, 41.45, "%veterin%sagrada%"),
    ("STARBUCKS", 2.10, 41.35, 2.25, 41.45, "%starbucks%sagrada%"),
]

for label, x0, y0, x1, y1, pat in PROBES:
    cur = con.execute(SQL, [SRC, x0, x1, y0, y1, pat])
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, t)) for t in cur.fetchall()]
    print(f"\n{'='*90}\n### {label}  pattern={pat!r}  n={len(rows)}\n{'='*90}")
    for r in rows[:3]:
        print(json.dumps({k: (str(v) if not isinstance(v, (int, float, bool, type(None))) else v)
                          for k, v in r.items()}, indent=2, ensure_ascii=False))
