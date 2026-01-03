-- Download Overture Maps divisions globally
-- Run with: ./scripts/download_divisions.sh (fetches latest release automatically)
--
-- Output: exports/divisions-global.parquet
-- Expected: ~4.3M records
--
-- Note: __OVERTURE_RELEASE__ is a placeholder substituted at runtime.
-- The download_divisions.sh script fetches the latest release version from the
-- Overture STAC catalog and replaces this placeholder via sed before execution.
-- Example: sed "s|__OVERTURE_RELEASE__|2025-01-01.0|g" ... | duckdb
--
-- TODO: Future iteration - download raw data first, then filter/transform in a
-- separate step. This would avoid re-downloading when tweaking search_text logic.

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
        -- Search text for FTS - focused on key searchable terms
        -- Includes: primary, short names (NYC), English common/alternate, region, country
        -- Excludes: multilingual translations to keep BM25 scoring balanced
        -- TODO: Consider language-specific shards for full multilingual search
        LOWER(ARRAY_TO_STRING(
            LIST_DISTINCT(
                LIST_FILTER(
                    STRING_SPLIT(
                        CONCAT_WS(' ',
                            -- Primary name (the main searchable name)
                            names.primary,
                            -- Short names with null language (e.g., "NYC", "LA")
                            COALESCE(ARRAY_TO_STRING(
                                list_transform(
                                    list_filter(names.rules, x -> x.variant = 'short' AND x.language IS NULL),
                                    x -> x.value
                                ),
                                ' '
                            ), ''),
                            -- English common name (names.common is MAP<language, value>)
                            COALESCE(list_extract(map_extract(names.common, 'en'), 1), ''),
                            -- Official names with null language (e.g., "New York" for NYC)
                            COALESCE(ARRAY_TO_STRING(
                                list_transform(
                                    list_filter(names.rules, x -> x.variant = 'official' AND x.language IS NULL),
                                    x -> x.value
                                ),
                                ' '
                            ), ''),
                            -- Alternate names with null/English language (e.g., "New York City", "Big Apple")
                            COALESCE(ARRAY_TO_STRING(
                                list_transform(
                                    list_filter(names.rules, x -> x.variant = 'alternate' AND (x.language IS NULL OR x.language LIKE 'en%')),
                                    x -> x.value
                                ),
                                ' '
                            ), ''),
                            -- Region code (e.g., "MA" for US)
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
