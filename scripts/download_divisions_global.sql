-- Download Overture Maps divisions globally
-- Run with: ./scripts/download_divisions.sh (fetches latest release automatically)
--
-- Output: exports/divisions-global.parquet
-- Contents: countries, regions, all counties/local-admin divisions, and
-- localities with population >10k or a Wikidata ID (the latter are pruned by
-- the shard build when they lack sufficient Wikimedia importance).
--
-- Note: __OVERTURE_RELEASE__ is a placeholder substituted at runtime.
-- The download_divisions.sh script fetches the latest release version from the
-- Overture STAC catalog and replaces this placeholder via sed before execution.
-- Example: sed "s|__OVERTURE_RELEASE__|2025-01-01.0|g" ... | duckdb
--
-- TODO: Future iteration - download raw data first, then filter/transform in a
-- separate step. This would avoid re-downloading when tweaking search column logic.

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
-- The CLI session is in-memory, which disables spilling by default;
-- without a temp_directory the big spatial join OOMs instead of going
-- out-of-core. Fewer threads also bounds parallel pipeline buffers.
SET temp_directory = '/tmp/duckdb_spill.tmp';
SET threads = 2;

.timer on

-- Extract global divisions for forward place and administrative-area search.
-- Subtypes: country, dependency, region, county, localadmin, locality,
--           macrohood, neighborhood, microhood
-- Note: version field increments each Overture release when feature changes
--
-- Country/region name lookups:
-- We join with country and region divisions to get their full names for search.
-- This allows "cambridge uk" to match Cambridge, GB-ENG because search_context
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
        -- Wikidata QID (e.g., "Q60") - joined against the Nominatim wikimedia
        -- importance file at shard build time (docs/ranking-research.md, P1)
        d.wikidata,
        -- Capital flags from capital_of_divisions (STRUCT(division_id, subtype)[]):
        -- used for the capital bonus in the precomputed importance (P2)
        COALESCE(len(list_filter(d.capital_of_divisions, x -> x.subtype = 'country')) > 0, false) as is_country_capital,
        COALESCE(len(list_filter(d.capital_of_divisions, x -> x.subtype = 'region')) > 0, false) as is_region_capital,
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
        -- search_name: the distinct names of THIS place, ';'-separated so
        -- name boundaries survive (FTS column 1, weighted highest -
        -- docs/ranking-research.md P0/P4). Keeping multi-word alternate
        -- names intact ("mexico city" for Ciudad de México) lets the
        -- worker's alt-name ladder phrase-match them; a flattened token bag
        -- can never hit the exact/prefix rungs. The FTS unicode61 tokenizer
        -- treats ';' as a separator, so indexing is unchanged.
        -- Includes: primary, short names (NYC), English common/official/alternate
        -- Excludes: multilingual translations to keep BM25 scoring balanced
        -- TODO: Consider language-specific shards for full multilingual search
        ARRAY_TO_STRING(
            LIST_DISTINCT(
                LIST_TRANSFORM(
                    LIST_FILTER(
                        -- Primary name (the main searchable name)
                        [d.names.primary]
                        -- Short names with null language (e.g., "NYC", "LA")
                        || COALESCE(list_transform(
                               list_filter(d.names.rules, x -> x.variant = 'short' AND x.language IS NULL),
                               x -> x.value), [])
                        -- English common name (names.common is MAP<language, value>)
                        || [list_extract(map_extract(d.names.common, 'en'), 1)]
                        -- Official names with null language (e.g., "New York" for NYC)
                        || COALESCE(list_transform(
                               list_filter(d.names.rules, x -> x.variant = 'official' AND x.language IS NULL),
                               x -> x.value), [])
                        -- Alternate names with null/English language (e.g., "New York City", "Big Apple")
                        || COALESCE(list_transform(
                               list_filter(d.names.rules, x -> x.variant = 'alternate' AND (x.language IS NULL OR x.language LIKE 'en%')),
                               x -> x.value), []),
                        x -> x IS NOT NULL AND TRIM(x) != ''
                    ),
                    -- ';' is the name separator; scrub it from name text
                    x -> REPLACE(LOWER(x), ';', ' ')
                )
            ), ';'
        ) as search_name,
        -- search_context: parent names + region/country codes (FTS column 3,
        -- weighted lowest). Enables "cambridge uk" / "boston ma" searches
        -- without letting context hits score like name hits.
        -- Note: raw region codes ("US-MA") tokenize to "us" + "ma" under
        -- unicode61, so both code forms are searchable.
        NULLIF(LOWER(ARRAY_TO_STRING(
            LIST_DISTINCT(
                LIST_FILTER(
                    STRING_SPLIT(
                        CONCAT_WS(' ',
                            -- Region code (e.g., "US-MA")
                            d.region,
                            -- Country code
                            d.country,
                            -- Parent division names (enables "cambridge uk" searches)
                            cn.country_name,   -- e.g., "United Kingdom", "United States"
                            cn.country_common, -- e.g., "United Kingdom", "United States" (from common.en)
                            cn.country_short,  -- e.g., "UK", "USA", "U.S." (from short names)
                            rn.region_name     -- e.g., "England", "Massachusetts"
                        ), ' '
                    ),
                    x -> x IS NOT NULL AND x != ''
                )
            ), ' '
        )), '') as search_context
    FROM read_parquet(
        's3://overturemaps-us-west-2/release/__OVERTURE_RELEASE__/theme=divisions/type=division/*',
        hive_partitioning = true
    ) d
    LEFT JOIN country_names cn ON d.country = cn.country_code
    LEFT JOIN region_names rn ON d.region = rn.region_code
    -- Administrative divisions are intentionally unfiltered: counties and
    -- local-admin areas are a comparatively small, useful search tier and
    -- their type prior keeps them below a similarly matched city. Localities
    -- use a population bar, OR any Wikidata QID. The QID rows are over-fetched
    -- on purpose — famous-but-small places (Gettysburg) have no population
    -- signal here; enrich_parquet_with_wiki_importance joins their Wikimedia
    -- importance and prunes the non-famous ones.
    WHERE (d.subtype IN ('country', 'region', 'county', 'localadmin')
           OR (d.subtype = 'locality' AND (d.population > 10000 OR d.wikidata IS NOT NULL)))
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
