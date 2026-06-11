-- Download Overture Maps divisions globally
-- Run with: ./scripts/download_divisions.sh (fetches latest release automatically)
--
-- Output: exports/divisions-global.parquet
-- Expected: ~500 records (country/region/locality with population >1M)
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
SET memory_limit = '12GB';
-- Output row order is irrelevant (shard builds re-query); preserving
-- insertion order is the main memory amplifier in COPY under DuckDB 1.5.
SET preserve_insertion_order = false;

.timer on

-- Extract global divisions (cities, towns, neighborhoods, counties)
-- Subtypes: country, dependency, region, county, localadmin, locality,
--           macrohood, neighborhood, microhood
-- Note: version field increments each Overture release when feature changes
--
-- Country/region name lookups:
-- We join with country and region divisions to get their full names for search.
-- This allows "cambridge uk" to match Cambridge, GB-ENG because search_text
-- includes "united kingdom" and "england", not just the codes "gb" and "gb-eng".
COPY (
    WITH
    -- Lookup table for country names (subtype='country')
    -- Extracts: country code -> primary name, short names (UK, USA), common English name
    country_names AS (
        SELECT
            country as country_code,  -- Country divisions have 'country' = ISO code (e.g., "US", "GB")
            names.primary as country_name,
            -- Common name in English (e.g., "United States", "United Kingdom")
            COALESCE(list_extract(map_extract(names.common, 'en'), 1), '') as country_common,
            -- Short names with null/English language (e.g., "UK", "USA", "U.S.")
            COALESCE(ARRAY_TO_STRING(
                list_transform(
                    list_filter(names.rules, x -> x.variant = 'short' AND (x.language IS NULL OR x.language LIKE 'en%')),
                    x -> x.value
                ),
                ' '
            ), '') as country_short
        FROM read_parquet(
            's3://overturemaps-us-west-2/release/__OVERTURE_RELEASE__/theme=divisions/type=division/*',
            hive_partitioning = true
        )
        WHERE subtype = 'country'
    ),
    -- Lookup table for region names (subtype='region')
    -- Extracts: region code -> primary name (e.g., "US-MA" -> "Massachusetts")
    region_names AS (
        SELECT
            region as region_code,  -- Region divisions have 'region' = ISO code (e.g., "US-MA", "GB-ENG")
            names.primary as region_name
        FROM read_parquet(
            's3://overturemaps-us-west-2/release/__OVERTURE_RELEASE__/theme=divisions/type=division/*',
            hive_partitioning = true
        )
        WHERE subtype = 'region'
          AND region IS NOT NULL  -- Some regions don't have codes (territories)
    )
    SELECT
        d.id as gers_id,
        d.version,
        d.names.primary as name,
        d.subtype,
        d.class,
        d.country,
        d.region,
        d.population,
        ST_X(d.geometry) as lon,
        ST_Y(d.geometry) as lat,
        d.bbox.xmin as bbox_xmin,
        d.bbox.ymin as bbox_ymin,
        d.bbox.xmax as bbox_xmax,
        d.bbox.ymax as bbox_ymax,
        -- Build primary name based on available data
        CASE
            -- US format: "Boston, MA"
            WHEN d.country = 'US' AND d.region IS NOT NULL THEN
                CONCAT(d.names.primary, ', ', REPLACE(d.region, 'US-', ''))
            -- Other countries with region: "London, GB-ENG"
            WHEN d.region IS NOT NULL THEN
                CONCAT(d.names.primary, ', ', d.region)
            -- Fallback: just the name and country
            ELSE
                CONCAT(d.names.primary, ', ', d.country)
        END as primary_name,
        -- Search text for FTS - focused on key searchable terms
        -- Includes: primary, short names (NYC), English common/alternate, region, country
        -- Also includes parent division names (country name, region name) for hierarchy search
        -- Excludes: multilingual translations to keep BM25 scoring balanced
        -- TODO: Consider language-specific shards for full multilingual search
        LOWER(ARRAY_TO_STRING(
            LIST_DISTINCT(
                LIST_FILTER(
                    STRING_SPLIT(
                        CONCAT_WS(' ',
                            -- Primary name (the main searchable name)
                            d.names.primary,
                            -- Short names with null language (e.g., "NYC", "LA")
                            COALESCE(ARRAY_TO_STRING(
                                list_transform(
                                    list_filter(d.names.rules, x -> x.variant = 'short' AND x.language IS NULL),
                                    x -> x.value
                                ),
                                ' '
                            ), ''),
                            -- English common name (names.common is MAP<language, value>)
                            COALESCE(list_extract(map_extract(d.names.common, 'en'), 1), ''),
                            -- Official names with null language (e.g., "New York" for NYC)
                            COALESCE(ARRAY_TO_STRING(
                                list_transform(
                                    list_filter(d.names.rules, x -> x.variant = 'official' AND x.language IS NULL),
                                    x -> x.value
                                ),
                                ' '
                            ), ''),
                            -- Alternate names with null/English language (e.g., "New York City", "Big Apple")
                            COALESCE(ARRAY_TO_STRING(
                                list_transform(
                                    list_filter(d.names.rules, x -> x.variant = 'alternate' AND (x.language IS NULL OR x.language LIKE 'en%')),
                                    x -> x.value
                                ),
                                ' '
                            ), ''),
                            -- Region code (e.g., "MA" for US)
                            CASE WHEN d.country = 'US' AND d.region IS NOT NULL
                                THEN REPLACE(d.region, 'US-', '')
                                ELSE d.region
                            END,
                            -- Country code
                            d.country,
                            -- Parent division names (NEW - enables "cambridge uk" searches)
                            cn.country_name,   -- e.g., "United Kingdom", "United States"
                            cn.country_common, -- e.g., "United Kingdom", "United States" (from common.en)
                            cn.country_short,  -- e.g., "UK", "USA", "U.S." (from short names)
                            rn.region_name     -- e.g., "England", "Massachusetts"
                        ), ' '
                    ),
                    x -> x IS NOT NULL AND x != ''
                )
            ), ' '
        )) as search_text
    FROM read_parquet(
        's3://overturemaps-us-west-2/release/__OVERTURE_RELEASE__/theme=divisions/type=division/*',
        hive_partitioning = true
    ) d
    LEFT JOIN country_names cn ON d.country = cn.country_code
    LEFT JOIN region_names rn ON d.region = rn.region_code
    WHERE (d.subtype IN ('country', 'region') OR (d.subtype = 'locality' AND d.population > 10000))
      AND d.names.primary IS NOT NULL
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
