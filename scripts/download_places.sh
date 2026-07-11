#!/bin/bash
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
RELEASE="$1"
LIMIT="$2"
if [ -n "$RELEASE" ]; then echo "Using release: $RELEASE"; else
RELEASE=$(python3 "$SCRIPT_DIR/stac.py" | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}\.[0-9]+' | head -1 || true)
if [ -z "$RELEASE" ]; then echo "ERROR fetch release"; exit 1; fi
echo "Using release: $RELEASE"; fi
mkdir -p "$PROJECT_DIR/exports"; cd "$PROJECT_DIR"
if [ -n "$LIMIT" ]; then
echo "Downloading top $LIMIT places by confidence for CA..."
python3 << PY
import duckdb
release="$RELEASE"
limit=int("$LIMIT")
con=duckdb.connect()
con.execute("INSTALL httpfs; LOAD httpfs; INSTALL spatial; LOAD spatial; SET s3_region='us-west-2'; SET memory_limit='8GB'; SET threads=2;")
con.execute(f"""
COPY (
    SELECT id as gers_id, version, names.primary as primary_name,
    ST_X(geometry) as lon, ST_Y(geometry) as lat,
    bbox.xmin as bbox_xmin, bbox.ymin as bbox_ymin, bbox.xmax as bbox_xmax, bbox.ymax as bbox_ymax,
    COALESCE(addresses[1].country, '') as country,
    COALESCE(addresses[1].region, '') as region,
    COALESCE(addresses[1].locality, '') as locality,
    COALESCE(addresses[1].postcode, '') as postcode,
    COALESCE(addresses[1].freeform, '') as freeform_address,
    categories.primary as category_primary, basic_category,
    taxonomy.primary as taxonomy_primary,
    taxonomy.hierarchy as taxonomy_hierarchy,
    brand.names.primary as brand_name, brand.wikidata as brand_wikidata,
    confidence,
    operating_status,
    (list_extract(list_filter(sources, lambda s: COALESCE(s.property, '') = ''), 1)).dataset as root_source_dataset,
    (list_extract(list_filter(sources, lambda s: COALESCE(s.property, '') = ''), 1)).update_time as root_source_update_time,
    (list_extract(list_filter(sources, lambda s: COALESCE(s.property, '') = ''), 1)).confidence as root_source_confidence,
    LEN(websites) as website_count,
    LEN(socials) as social_count,
    LEN(phones) as phone_count,
    LEN(names.common) as common_name_count,
    LOWER(CONCAT_WS(' ', names.primary, brand.names.primary, categories.primary, basic_category)) as search_name_base,
    LOWER(CONCAT_WS(' ', addresses[1].locality, addresses[1].region, addresses[1].country, categories.primary, basic_category)) as search_context_base
    FROM read_parquet('s3://overturemaps-us-west-2/release/{release}/theme=places/type=place/*', hive_partitioning=true)
    WHERE bbox.xmin BETWEEN -124.5 AND -114.0 AND bbox.ymin BETWEEN 32.5 AND 42.1
    AND names.primary IS NOT NULL AND COALESCE(operating_status, 'open') != 'permanently_closed'
    ORDER BY confidence DESC NULLS LAST LIMIT {limit}
) TO 'exports/places-CA.parquet' (FORMAT PARQUET, COMPRESSION ZSTD);
PY
else
echo "Downloading CA places full bbox..."
sed "s|__OVERTURE_RELEASE__|$RELEASE|g" scripts/download_places.sql | duckdb
fi
ROW_COUNT=$(duckdb -csv -noheader -c "SELECT COUNT(*) FROM read_parquet('$PROJECT_DIR/exports/places-CA.parquet')" 2>/dev/null | tr -d '[:space:]')
echo "CA places: exports/places-CA.parquet ($ROW_COUNT rows)"
