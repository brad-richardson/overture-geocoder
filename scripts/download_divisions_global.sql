-- Download Overture Maps divisions globally
-- Run with: duckdb < scripts/download_divisions_global.sql
--
-- Output: exports/divisions-global.parquet
-- Expected: ~4.3M records

-- Install and load required extensions
INSTALL httpfs;
LOAD httpfs;
INSTALL spatial;
LOAD spatial;

-- Configure S3 for anonymous access (Overture is public)
SET s3_region = 'us-west-2';
SET memory_limit = '8GB';

.timer on

-- Extract global divisions (cities, towns, neighborhoods, counties)
-- Subtypes: country, dependency, region, county, localadmin, locality,
--           macrohood, neighborhood, microhood
COPY (
    SELECT
        id as gers_id,
        names.primary as name,
        subtype,
        class,
        country,
        region,
        population,
        ST_X(geometry) as lon,
        ST_Y(geometry) as lat,
        bbox.xmin as bbox_xmin,
        bbox.ymin as bbox_ymin,
        bbox.xmax as bbox_xmax,
        bbox.ymax as bbox_ymax,
        -- Build display name based on available data
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
        END as display_name,
        -- Search text (lowercase name)
        LOWER(names.primary) as search_text
    FROM read_parquet(
        's3://overturemaps-us-west-2/release/2025-12-17.0/theme=divisions/type=division/*',
        hive_partitioning = true
    )
    WHERE subtype IN ('locality', 'localadmin', 'neighborhood', 'macrohood', 'county')
      AND names.primary IS NOT NULL
)
TO 'exports/divisions-global.parquet' (FORMAT PARQUET, COMPRESSION ZSTD);

-- Show count and breakdown
SELECT COUNT(*) as total_divisions FROM read_parquet('exports/divisions-global.parquet');

SELECT subtype, COUNT(*) as count
FROM read_parquet('exports/divisions-global.parquet')
GROUP BY subtype
ORDER BY count DESC;

SELECT country, COUNT(*) as count
FROM read_parquet('exports/divisions-global.parquet')
GROUP BY country
ORDER BY count DESC
LIMIT 20;
