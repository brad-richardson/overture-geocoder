INSTALL httpfs;
LOAD httpfs;
INSTALL spatial;
LOAD spatial;
SET s3_region = 'us-west-2';
SET memory_limit = '8GB';
SET preserve_insertion_order = false;
SET temp_directory = '/tmp/duckdb_spill.tmp';
SET threads = 2;
.timer on
COPY (
    SELECT
        id as gers_id,
        version,
        names.primary as primary_name,
        ST_X(geometry) as lon,
        ST_Y(geometry) as lat,
        bbox.xmin as bbox_xmin,
        bbox.ymin as bbox_ymin,
        bbox.xmax as bbox_xmax,
        bbox.ymax as bbox_ymax,
        COALESCE(addresses[1].country, '') as country,
        COALESCE(addresses[1].region, '') as region,
        COALESCE(addresses[1].locality, '') as locality,
        categories.primary as category_primary,
        basic_category,
        brand.names.primary as brand_name,
        brand.wikidata as brand_wikidata,
        confidence,
        LOWER(CONCAT_WS(' ', names.primary, brand.names.primary, categories.primary, basic_category)) as search_name_base,
        LOWER(CONCAT_WS(' ', addresses[1].locality, addresses[1].region, addresses[1].country, categories.primary, basic_category)) as search_context_base
    FROM read_parquet('s3://overturemaps-us-west-2/release/__OVERTURE_RELEASE__/theme=places/type=place/*', hive_partitioning=true)
    WHERE bbox.xmin BETWEEN -124.5 AND -114.0
      AND bbox.ymin BETWEEN 32.5 AND 42.1
      AND names.primary IS NOT NULL
      AND COALESCE(operating_status, 'open') != 'permanently_closed'
) TO 'exports/places-CA.parquet' (FORMAT PARQUET, COMPRESSION ZSTD);
