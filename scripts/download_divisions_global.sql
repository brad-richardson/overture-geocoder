-- Download Overture Maps divisions globally
-- Run with: ./scripts/download_divisions.sh (fetches latest release automatically)
--
-- Output: exports/divisions-global.parquet
-- Expected: ~4.3M records
--
-- Note: __OVERTURE_RELEASE__ is substituted at runtime with the latest release

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
-- Note: version field increments each Overture release when feature changes
COPY (
    SELECT
        id as gers_id,
        version,
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
        -- Build primary name based on available data
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
        -- Search text includes all name variants + enclosing divisions for FTS
        -- Uses LIST_DISTINCT to remove duplicate words
        LOWER(ARRAY_TO_STRING(
            LIST_DISTINCT(
                LIST_FILTER(
                    STRING_SPLIT(
                        CONCAT_WS(' ',
                            names.primary,
                            -- Common names (multilingual translations)
                            COALESCE(ARRAY_TO_STRING(MAP_VALUES(names.common), ' '), ''),
                            -- All name variants from rules array (official, alternate, short)
                            COALESCE(
                                ARRAY_TO_STRING(
                                    LIST_TRANSFORM(names.rules, r -> r.value),
                                    ' '
                                ),
                                ''
                            ),
                            -- Hierarchy names (enclosing divisions: country, region, county, etc.)
                            COALESCE(
                                ARRAY_TO_STRING(
                                    LIST_TRANSFORM(hierarchies[1], h -> h.name),
                                    ' '
                                ),
                                ''
                            ),
                            -- Region code (e.g., "MA" for US, or full region code)
                            CASE WHEN country = 'US' AND region IS NOT NULL
                                THEN REPLACE(region, 'US-', '')
                                ELSE region
                            END,
                            -- Country code
                            country
                        ), ' '
                    ),
                    x -> x IS NOT NULL AND x != ''
                )
            ), ' '
        )) as search_text
    FROM read_parquet(
        's3://overturemaps-us-west-2/release/__OVERTURE_RELEASE__/theme=divisions/type=division/*',
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
