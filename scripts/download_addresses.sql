-- Download and filter Overture Maps addresses for Massachusetts
-- Run with: ./scripts/download_addresses.sh [STATE] [RELEASE]
-- Or manually: sed 's/__OVERTURE_RELEASE__/2025-01-01.0/g' scripts/download_addresses.sql | duckdb
--
-- Prerequisites:
--   brew install duckdb  (or download from duckdb.org)
--
-- Output: exports/US-MA.parquet (~50-100MB)

-- Install and load required extensions
INSTALL httpfs;
LOAD httpfs;
INSTALL spatial;
LOAD spatial;

-- Configure S3 for anonymous access (Overture is public)
SET s3_region = 'us-west-2';

-- Set memory limit for large operations
SET memory_limit = '4GB';
SET threads = 4;

-- Show progress
.timer on

-- Extract Massachusetts addresses with display name and search text
-- address_levels schema: [{value: state}, {value: city}]
-- Index 0 = state abbreviation (e.g., "MA")
-- Index 1 = city (e.g., "Boston")
COPY (
    SELECT
        id as gers_id,
        version,
        ST_X(geometry) as lon,
        ST_Y(geometry) as lat,
        bbox.xmin as bbox_xmin,
        bbox.ymin as bbox_ymin,
        bbox.xmax as bbox_xmax,
        bbox.ymax as bbox_ymax,
        postcode,
        street,
        number,
        unit,
        country,
        -- Preserve the feature-level external source. Property-specific
        -- Overture-derived confidence/status entries are intentionally not
        -- counted as independent provenance.
        (list_extract(list_filter(sources, lambda x: x.property = ''), 1)).dataset
            as source_dataset,
        (list_extract(list_filter(sources, lambda x: x.property = ''), 1)).update_time
            as source_update_time,
        (list_extract(list_filter(sources, lambda x: x.property = ''), 1)).confidence
            as source_confidence,
        -- Extract city and state from address_levels array
        address_levels[1].value as state,
        address_levels[2].value as city,
        postal_city,
        -- Build primary name: "123 Main St, Boston, MA 02101"
        CONCAT_WS(', ',
            NULLIF(CONCAT_WS(' ', number, street, unit), ''),
            COALESCE(address_levels[2].value, postal_city),
            CONCAT(address_levels[1].value, ' ', postcode)
        ) as primary_name,
        -- Build search text (lowercase, for FTS indexing)
        -- Includes: number, street, unit, city, state, postcode, country
        LOWER(CONCAT_WS(' ',
            COALESCE(number, ''),
            COALESCE(street, ''),
            COALESCE(unit, ''),
            COALESCE(address_levels[2].value, postal_city, ''),
            COALESCE(address_levels[1].value, ''),  -- state abbreviation (MA)
            COALESCE(postcode, ''),
            'us'  -- country (addresses are US-only currently)
        )) as search_text
    FROM read_parquet(
        's3://overturemaps-us-west-2/release/__OVERTURE_RELEASE__/theme=addresses/type=address/*',
        hive_partitioning = true
    )
    WHERE country = 'US'
      AND address_levels[1].value = '__STATE__'
)
TO 'exports/US-__STATE__.parquet' (FORMAT PARQUET, COMPRESSION ZSTD);

-- Show count
SELECT COUNT(*) as address_count FROM read_parquet('exports/US-__STATE__.parquet');
