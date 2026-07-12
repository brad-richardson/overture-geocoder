-- Materialize the pinned Overture country claims used by the research-only
-- exact-country decision artifact.
--
-- Required substitutions:
--   __OVERTURE_RELEASE__  e.g. 2026-06-17.0
--   __OUTPUT_PATH__       local parquet output (single quotes are not supported)
--
-- Example:
--   sed -e 's|__OVERTURE_RELEASE__|2026-06-17.0|g' \
--       -e 's|__OUTPUT_PATH__|/tmp/overture-country-2026-06-17.0.parquet|g' \
--       scripts/extract_country_router.sql | duckdb
--
-- The builder hashes this complete local output. The query joins by immutable
-- parent ID, not by country code, and retains both land and territorial claims,
-- including the release's observed dual-true rows and synthetic X* codes.

INSTALL httpfs;
LOAD httpfs;

SET s3_region = 'us-west-2';
SET threads = 2;
SET memory_limit = '4GB';
SET preserve_insertion_order = false;
SET temp_directory = '/tmp/overture-country-router-duckdb-spill';

COPY (
    SELECT
        a.id AS area_id,
        '__OVERTURE_RELEASE__' AS overture_release,
        a.division_id,
        a.version AS area_version,
        d.version AS division_version,
        a.country,
        d.country AS division_country,
        d.names.primary AS primary_name,
        a.class AS area_class,
        d.class AS division_class,
        a.is_land,
        a.is_territorial,
        a.geometry,
        a.bbox.xmin AS bbox_xmin,
        a.bbox.ymin AS bbox_ymin,
        a.bbox.xmax AS bbox_xmax,
        a.bbox.ymax AS bbox_ymax,
        to_json(a.sources) AS area_sources_json,
        to_json(d.sources) AS division_sources_json,
        to_json(d.perspectives) AS division_perspectives_json,
        to_json(d.hierarchies) AS hierarchies_json
    FROM read_parquet(
        's3://overturemaps-us-west-2/release/__OVERTURE_RELEASE__/theme=divisions/type=division_area/*',
        hive_partitioning = true
    ) AS a
    LEFT JOIN read_parquet(
        's3://overturemaps-us-west-2/release/__OVERTURE_RELEASE__/theme=divisions/type=division/*',
        hive_partitioning = true
    ) AS d ON d.id = a.division_id AND d.subtype = 'country'
    WHERE a.subtype = 'country'
    ORDER BY a.id
) TO '__OUTPUT_PATH__' (FORMAT PARQUET, COMPRESSION ZSTD);

SELECT
    count(*) AS claim_rows,
    count(DISTINCT country) AS country_codes,
    count(*) FILTER (WHERE is_land) AS land_rows,
    count(*) FILTER (WHERE is_territorial) AS territorial_rows,
    count(*) FILTER (WHERE is_land AND is_territorial) AS dual_rows,
    count(*) FILTER (
        WHERE NOT coalesce(is_land, false)
          AND NOT coalesce(is_territorial, false)
    ) AS neither_rows,
    count(*) FILTER (WHERE country LIKE 'X%') AS synthetic_rows,
    count(*) FILTER (WHERE division_country IS NULL) AS missing_parent_rows,
    count(*) FILTER (
        WHERE division_country != country
    ) AS parent_country_mismatch_rows
FROM read_parquet('__OUTPUT_PATH__');
