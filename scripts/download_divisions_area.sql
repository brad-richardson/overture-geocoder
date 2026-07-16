-- Download Overture Maps divisions for reverse geocoding.
--
-- Includes populated localities (cities and towns) alongside broad
-- administrative containers. The initial 50k population bar gives useful
-- city coverage while avoiding the millions of small locality/neighborhood
-- polygons that do not fit the build runner's memory envelope.
--
-- JOINs division (point) with division_area (polygon)
-- Run with: ./scripts/download_divisions.sh (uses same shell wrapper)
--
-- Output: exports/divisions-reverse.parquet
--
-- Note: __OVERTURE_RELEASE__, __DIVISION_FILTER__, __AREA_FILTER__, and
-- __OUTPUT_PATH__ are placeholders substituted at runtime. Production renders
-- both filters as TRUE; the merge smoke renders validated Monaco predicates.
-- The download_divisions.sh script fetches the latest release version from the
-- Overture STAC catalog and replaces this placeholder via sed before execution.
-- Example: sed "s|__OVERTURE_RELEASE__|2025-01-01.0|g" ... | duckdb

-- Install and load required extensions
INSTALL httpfs;
LOAD httpfs;
INSTALL spatial;
LOAD spatial;
-- H3 disabled for now - bbox is sufficient for reverse geocoding
-- INSTALL h3;
-- LOAD h3;

-- Configure S3 for anonymous access (Overture is public)
SET s3_region = 'us-west-2';
SET memory_limit = '12GB';
-- Output row order is irrelevant (shard builds re-query); preserving
-- insertion order is the main memory amplifier in COPY under DuckDB 1.5.
SET preserve_insertion_order = false;
-- The CLI session is in-memory, which disables spilling by default;
-- without a temp_directory the big spatial join OOMs instead of going
-- out-of-core. Fewer threads also bounds parallel pipeline buffers.
SET temp_directory = '/tmp/duckdb_spill.tmp';
SET threads = 2;

.timer on

-- Extract divisions joined with division_area for reverse geocoding
-- Key data sources:
--   division -> lat/lon (curated point), names, population, version
--   division_area -> bbox, H3 cells, area (from polygon geometry)
COPY (
    WITH divisions AS MATERIALIZED (
        SELECT
            id as gers_id,
            version,
            subtype,
            country,
            region,
            population,
            names.primary as name,
            -- Lat/lon from division's curated point geometry
            ST_X(geometry) as lon,
            ST_Y(geometry) as lat,
            -- Build primary_name based on available data
            CASE
                -- US format: "Boston, MA"
                WHEN country = 'US' AND region IS NOT NULL THEN
                    CONCAT(names.primary, ', ', REPLACE(region, 'US-', ''))
                -- Other countries with region: "London, GB-ENG"
                WHEN region IS NOT NULL THEN
                    CONCAT(names.primary, ', ', region)
                -- Fallback: just the name and country
                ELSE
                    CONCAT(names.primary, ', ', country)
            END as primary_name,
            -- Materialized primary-hierarchy ancestor chain: the division GERS
            -- IDs of hierarchies[1] (DuckDB 1-indexed first path) as an
            -- ORDERED JSON array root -> ... -> self. Verified against release
            -- 2026-06-17.0: every reverse-eligible division carries exactly
            -- one hierarchy path, so the first path IS the default/display
            -- hierarchy; the division itself is always the last element
            -- (55,517/55,517) and the root is the country (55,515/55,517; the
            -- two exceptions are territory roots, still root-first).
            -- list_transform preserves list order. The reverse worker parses
            -- this to test exact parentage (candidate.gers_id in the anchor's
            -- chain) without recursion, replacing the country/region-code
            -- coherence heuristic, and resolves same-subtype collisions by
            -- chain position so the primary path's parent wins. NULL when the
            -- source row carries no hierarchies; the worker then falls back to
            -- the legacy heuristic for that row.
            CASE
                WHEN hierarchies IS NULL OR len(hierarchies) = 0 THEN NULL
                ELSE CAST(
                    to_json(
                        list_transform(hierarchies[1], x -> x.division_id)
                    ) AS VARCHAR
                )
            END as lineage_ids
        FROM read_parquet(
            's3://overturemaps-us-west-2/release/__OVERTURE_RELEASE__/theme=divisions/type=division/*',
            hive_partitioning = true
        )
        WHERE names.primary IS NOT NULL
          AND (
              subtype IN ('country', 'region', 'county')
              OR (subtype = 'locality' AND COALESCE(population, 0) >= 50000)
          )
          AND (__DIVISION_FILTER__)
    ),
    areas_all AS (
        SELECT
            a.division_id,
            -- Bbox from polygon geometry (more accurate than division's pre-computed bbox)
            ST_XMin(a.geometry) as bbox_xmin,
            ST_YMin(a.geometry) as bbox_ymin,
            ST_XMax(a.geometry) as bbox_xmax,
            ST_YMax(a.geometry) as bbox_ymax,
            -- Area for ranking (smaller = more specific)
            ST_Area(a.geometry) as area
        FROM read_parquet(
            's3://overturemaps-us-west-2/release/__OVERTURE_RELEASE__/theme=divisions/type=division_area/*',
            hive_partitioning = true
        ) a
        JOIN divisions d ON a.division_id = d.gers_id
        -- The materialized eligible-ID join happens before spatial work, so
        -- excluded locality/neighborhood polygons never enter the window.
        WHERE a.subtype IN ('country', 'region', 'county', 'locality')
          AND (__AREA_FILTER__)
    )
    SELECT
        d.gers_id,
        d.version,
        d.subtype,
        d.primary_name,
        d.lat,
        d.lon,
        d.population,
        d.country,
        d.region,
        a.bbox_xmin,
        a.bbox_ymin,
        a.bbox_xmax,
        a.bbox_ymax,
        a.area,
        d.lineage_ids
    FROM divisions d
    JOIN areas_all a ON d.gers_id = a.division_id
)
TO '__OUTPUT_PATH__' (FORMAT PARQUET, COMPRESSION ZSTD);

-- Show count and breakdown
SELECT COUNT(*) as total_divisions FROM read_parquet('__OUTPUT_PATH__');

SELECT subtype, COUNT(*) as count
FROM read_parquet('__OUTPUT_PATH__')
GROUP BY subtype
ORDER BY count DESC;

-- Show sample records
SELECT gers_id, subtype, primary_name, lat, lon, area
FROM read_parquet('__OUTPUT_PATH__')
LIMIT 5;
