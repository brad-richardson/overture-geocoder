-- Download Overture Maps divisions for reverse geocoding
-- JOINs division (point) with division_area (polygon)
-- Run with: ./scripts/download_divisions.sh (uses same shell wrapper)
--
-- Output: exports/divisions-reverse.parquet
-- Expected: ~450K records (divisions with matching areas)
--
-- Note: __OVERTURE_RELEASE__ is substituted at runtime with the latest release

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
SET memory_limit = '8GB';

.timer on

-- Extract divisions joined with division_area for reverse geocoding
-- Key data sources:
--   division -> lat/lon (curated point), names, population, version
--   division_area -> bbox, H3 cells, area (from polygon geometry)
COPY (
    WITH divisions AS (
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
            END as primary_name
        FROM read_parquet(
            's3://overturemaps-us-west-2/release/__OVERTURE_RELEASE__/theme=divisions/type=division/*',
            hive_partitioning = true
        )
        WHERE subtype IN ('country', 'region', 'county', 'localadmin', 'locality', 'neighborhood', 'macrohood')
          AND names.primary IS NOT NULL
    ),
    areas_all AS (
        SELECT
            division_id,
            -- Bbox from polygon geometry (more accurate than division's pre-computed bbox)
            ST_XMin(geometry) as bbox_xmin,
            ST_YMin(geometry) as bbox_ymin,
            ST_XMax(geometry) as bbox_xmax,
            ST_YMax(geometry) as bbox_ymax,
            -- Area for ranking (smaller = more specific)
            ST_Area(geometry) as area,
            -- Pick one area per division (prefer smallest/most specific)
            ROW_NUMBER() OVER (PARTITION BY division_id ORDER BY ST_Area(geometry) ASC) as rn
        FROM read_parquet(
            's3://overturemaps-us-west-2/release/__OVERTURE_RELEASE__/theme=divisions/type=division_area/*',
            hive_partitioning = true
        )
    ),
    areas AS (
        SELECT division_id, bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax, area
        FROM areas_all
        WHERE rn = 1
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
        a.area
    FROM divisions d
    JOIN areas a ON d.gers_id = a.division_id
)
TO 'exports/divisions-reverse.parquet' (FORMAT PARQUET, COMPRESSION ZSTD);

-- Show count and breakdown
SELECT COUNT(*) as total_divisions FROM read_parquet('exports/divisions-reverse.parquet');

SELECT subtype, COUNT(*) as count
FROM read_parquet('exports/divisions-reverse.parquet')
GROUP BY subtype
ORDER BY count DESC;

-- Show sample records
SELECT gers_id, subtype, primary_name, lat, lon, area
FROM read_parquet('exports/divisions-reverse.parquet')
LIMIT 5;
