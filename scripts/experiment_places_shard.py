#!/usr/bin/env python3
"""Build the non-promotable experimental Places (POI) shard.

This is a standalone prototype kept out of the production shard build
(build_shards.py): the extractor is fixed to a rectangular California-area
bounding box, not an exact US-CA state boundary, so its output cannot be
promoted to a real shard. Shared shard helpers (schema, alias generation,
STAC collection, hashing) are imported from build_shards so the experiment
tracks the production format.

Usage:
    python scripts/experiment_places_shard.py \
        --experimental-places-bbox-slice \
        --places-parquet exports/places-CA-bbox.parquet

Output:
    shards/{version}/
        places-experimental/EXPERIMENT-CA-BBOX-places.db
        places-collection.json
        build-meta.json
"""

from __future__ import annotations

import argparse
import platform
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import duckdb

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from build_shards import (  # noqa: E402
    DIVISIONS_INSERT_SQL,
    SHARDS_DIR,
    build_search_alias,
    build_shard_schema,
    generate_stac_collection,
    get_git_sha,
    get_version,
    hash_file,
    validate_region_code,
    version_timestamp,
    write_json,
)

# ---------------------------------------------------------------------------
# Places prototype constants
# ---------------------------------------------------------------------------
PLACES_CA_BBOX = {
    "xmin": -124.5,
    "xmax": -114.0,
    "ymin": 32.5,
    "ymax": 42.1,
}
PLACES_CA_BBOX_SLICE_ID = "EXPERIMENT-CA-BBOX-places"

PLACES_CONFIDENCE_WEIGHT = 0.5
PLACES_BRAND_BONUS = 0.20
PLACES_BRAND_WIKIDATA_BONUS = 0.10
PLACES_HIGH_CONFIDENCE_BONUS = 0.10
PLACES_HIGH_CONFIDENCE_THRESHOLD = 0.90

PLACES_CATEGORY_PRIOR = {
    "airport": 0.25,
    "national_park": 0.20,
    "university": 0.15,
    "hospital": 0.12,
    "stadium": 0.12,
    "museum": 0.10,
    "hotel": 0.05,
    "restaurant": 0.02,
}


def compute_places_importance(
    confidence: float | None,
    brand_name: str | None,
    brand_wikidata: str | None,
    category_primary: str | None,
    basic_category: str | None,
) -> float:
    conf = float(confidence) if confidence is not None else 0.5
    conf = max(0.0, min(1.0, conf))
    importance = conf * PLACES_CONFIDENCE_WEIGHT
    if brand_name:
        importance += PLACES_BRAND_BONUS
        if brand_wikidata:
            importance += PLACES_BRAND_WIKIDATA_BONUS
    if conf >= PLACES_HIGH_CONFIDENCE_THRESHOLD:
        importance += PLACES_HIGH_CONFIDENCE_BONUS
    cat = (category_primary or basic_category or "").lower()
    if cat in PLACES_CATEGORY_PRIOR:
        importance += PLACES_CATEGORY_PRIOR[cat]
    return min(1.0, importance)


def compute_places_stored_importance(
    ranking_strategy: str,
    confidence: float | None,
    brand_name: str | None,
    brand_wikidata: str | None,
    category_primary: str | None,
    basic_category: str | None,
) -> float:
    """Compute stored/query importance independently from sample selection."""
    if ranking_strategy == "neutral":
        return 0.5
    if ranking_strategy == "confidence":
        conf = float(confidence) if confidence is not None else 0.5
        return max(0.0, min(1.0, conf))
    if ranking_strategy == "experimental-prominence":
        return compute_places_importance(
            confidence,
            brand_name,
            brand_wikidata,
            category_primary,
            basic_category,
        )
    raise ValueError(f"unknown places ranking strategy: {ranking_strategy}")


def places_importance_sql(
    confidence_expr: str,
    brand_name_expr: str,
    brand_wikidata_expr: str,
    category_primary_expr: str,
    basic_category_expr: str,
) -> str:
    """Return the SQL equivalent of compute_places_importance()."""
    category_cases = " ".join(
        f"WHEN '{category}' THEN {prior}"
        for category, prior in PLACES_CATEGORY_PRIOR.items()
    )
    category_expr = (
        f"LOWER(COALESCE({category_primary_expr}, {basic_category_expr}, ''))"
    )
    return f"""
        LEAST(1.0,
            LEAST(1.0, GREATEST(0.0, COALESCE({confidence_expr}, 0.5)))
                * {PLACES_CONFIDENCE_WEIGHT}
            + CASE WHEN {brand_name_expr} IS NOT NULL AND {brand_name_expr} != ''
                THEN {PLACES_BRAND_BONUS} ELSE 0 END
            + CASE WHEN {brand_name_expr} IS NOT NULL AND {brand_name_expr} != ''
                         AND {brand_wikidata_expr} IS NOT NULL
                         AND {brand_wikidata_expr} != ''
                THEN {PLACES_BRAND_WIKIDATA_BONUS} ELSE 0 END
            + CASE WHEN COALESCE({confidence_expr}, 0.5)
                         >= {PLACES_HIGH_CONFIDENCE_THRESHOLD}
                THEN {PLACES_HIGH_CONFIDENCE_BONUS} ELSE 0 END
            + CASE {category_expr} {category_cases} ELSE 0 END
        )
    """.strip()


def _places_flat_columns(parquet_path: Path) -> set[str] | None:
    try:
        con = duckdb.connect()
        cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{str(parquet_path.resolve())}') LIMIT 0").fetchall()]
        con.close()
        needed = {"gers_id", "primary_name", "lat", "lon"}
        column_set = set(cols)
        return column_set if needed.issubset(column_set) else None
    except Exception:
        return None


def build_places_shard(
    parquet_path: Path | str,
    output_path: Path,
    version: str,
    region_code: str = "US-CA",
    limit: int | None = None,
    sampling_strategy: str | None = None,
    ranking_strategy: str = "confidence",
) -> dict:
    region_code = validate_region_code(region_code)
    if limit is not None and limit <= 0:
        raise ValueError("places limit must be greater than zero")
    if limit is not None and sampling_strategy not in {
        "confidence",
        "experimental-prominence",
    }:
        raise ValueError(
            "places sampling requires an explicit strategy: confidence or "
            "experimental-prominence"
        )
    if ranking_strategy not in {
        "neutral",
        "confidence",
        "experimental-prominence",
    }:
        raise ValueError(f"unknown places ranking strategy: {ranking_strategy}")

    if isinstance(parquet_path, str) and parquet_path.startswith("s3://"):
        parquet_str = parquet_path
        is_flat = False
        flat_columns: set[str] = set()
    else:
        pp = Path(parquet_path) if not isinstance(parquet_path, Path) else parquet_path
        try:
            exists = pp.exists()
        except Exception:
            exists = False
        if exists:
            parquet_str = str(pp.resolve())
            detected_columns = _places_flat_columns(pp)
            is_flat = detected_columns is not None
            flat_columns = detected_columns or set()
        else:
            parquet_str = str(parquet_path)
            is_flat = False
            flat_columns = set()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()

    con = duckdb.connect()
    db = sqlite3.connect(output_path)
    build_shard_schema(db)

    if is_flat:
        status_predicate = (
            "COALESCE(operating_status, 'open') != 'permanently_closed'"
            if "operating_status" in flat_columns
            else "TRUE"
        )
        importance_expr = places_importance_sql(
            "confidence",
            "brand_name",
            "brand_wikidata",
            "category_primary",
            "basic_category",
        )
        if limit:
            sampling_order = (
                f"{importance_expr} DESC, confidence DESC NULLS LAST, gers_id"
                if sampling_strategy == "experimental-prominence"
                else "confidence DESC NULLS LAST, gers_id"
            )
            query = f"""
                SELECT
                    gers_id, version, primary_name, lat, lon,
                    bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax,
                    country, region, locality,
                    category_primary, basic_category,
                    brand_name, brand_wikidata, confidence,
                    COALESCE(CAST(search_name_base AS VARCHAR), LOWER(primary_name)) as search_name_base,
                    COALESCE(CAST(search_context_base AS VARCHAR), LOWER(CONCAT_WS(' ', locality, region, country))) as search_context_base
                FROM read_parquet('{parquet_str}')
                WHERE {status_predicate}
                ORDER BY {sampling_order}
                LIMIT {int(limit)}
            """
        else:
            query = f"""
                SELECT
                    gers_id, version, primary_name, lat, lon,
                    bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax,
                    country, region, locality,
                    category_primary, basic_category,
                    brand_name, brand_wikidata, confidence,
                    COALESCE(CAST(search_name_base AS VARCHAR), LOWER(primary_name)) as search_name_base,
                    COALESCE(CAST(search_context_base AS VARCHAR), LOWER(CONCAT_WS(' ', locality, region, country))) as search_context_base
                FROM read_parquet('{parquet_str}')
                WHERE {status_predicate}
                ORDER BY gers_id
            """
        cursor = con.execute(query)
    else:
        if region_code != "US-CA":
            db.close()
            con.close()
            output_path.unlink(missing_ok=True)
            raise ValueError(
                "raw Overture places extraction currently supports only US-CA; "
                "provide a flattened, region-filtered parquet for other regions"
            )
        print("  Places source appears to be raw Overture places parquet, using nested extraction...")
        limit_clause = f"LIMIT {int(limit)}" if limit else ""
        importance_expr = places_importance_sql(
            "confidence",
            "brand.names.primary",
            "brand.wikidata",
            "categories.primary",
            "basic_category",
        )
        if limit:
            order_clause = (
                f"ORDER BY {importance_expr} DESC, confidence DESC NULLS LAST, id"
                if sampling_strategy == "experimental-prominence"
                else "ORDER BY confidence DESC NULLS LAST, id"
            )
        else:
            order_clause = "ORDER BY id"
        query = f"""
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
            FROM read_parquet('{parquet_str}', hive_partitioning=true)
            WHERE bbox.xmin BETWEEN {PLACES_CA_BBOX['xmin']} AND {PLACES_CA_BBOX['xmax']}
              AND bbox.ymin BETWEEN {PLACES_CA_BBOX['ymin']} AND {PLACES_CA_BBOX['ymax']}
              AND names.primary IS NOT NULL
              AND COALESCE(operating_status, 'open') != 'permanently_closed'
            {order_clause}
            {limit_clause}
        """
        con.execute("INSTALL spatial; LOAD spatial;")
        cursor = con.execute(query)

    count = 0
    bbox = [180.0, 90.0, -180.0, -90.0]
    FETCH_SIZE = 50000

    while True:
        rows = cursor.fetchmany(FETCH_SIZE)
        if not rows:
            break
        prepared = []
        for r in rows:
            try:
                (gers_id, version_num, primary_name, lat, lon,
                 bxmin, bymin, bxmax, bymax,
                 country, region, locality,
                 cat_primary, basic_cat,
                 brand_name, brand_wikidata, confidence,
                 search_name_base, search_context_base) = r
            except ValueError:
                continue
            if not gers_id or not primary_name:
                continue
            try:
                lat_f = float(lat)
                lon_f = float(lon)
                bxmin_f = float(bxmin)
                bymin_f = float(bymin)
                bxmax_f = float(bxmax)
                bymax_f = float(bymax)
            except Exception:
                continue

            bbox[0] = min(bbox[0], bxmin_f)
            bbox[1] = min(bbox[1], bymin_f)
            bbox[2] = max(bbox[2], bxmax_f)
            bbox[3] = max(bbox[3], bymax_f)

            search_name = (search_name_base or primary_name.lower()).strip()
            if not search_name:
                search_name = primary_name.lower()
            search_context = (search_context_base or f"{locality} {region} {country}".strip().lower()).strip()
            search_alias = build_search_alias(primary_name, search_name)

            if isinstance(version_num, int):
                ver = version_num
            else:
                try:
                    ver = int(version_num) if version_num is not None else 0
                except Exception:
                    ver = 0

            importance = compute_places_stored_importance(
                ranking_strategy,
                confidence,
                brand_name,
                brand_wikidata,
                cat_primary,
                basic_cat,
            )

            c = (country or "US")[:2] or "US"
            reg = region or "US-CA"
            if len(reg) == 2 and c == "US":
                reg = f"US-{reg}"

            prepared.append((
                gers_id, ver, "place", primary_name, lat_f, lon_f,
                bxmin_f, bymin_f, bxmax_f, bymax_f,
                None, c, reg,
                search_name, search_alias, search_context, importance,
            ))

        if prepared:
            db.executemany(DIVISIONS_INSERT_SQL, prepared)
            count += len(prepared)

    db.execute("INSERT OR REPLACE INTO metadata VALUES ('version', ?)", (version,))
    db.execute("INSERT OR REPLACE INTO metadata VALUES ('region', ?)", (region_code,))
    db.execute("INSERT OR REPLACE INTO metadata VALUES ('type', ?)", ("places",))
    db.execute(
        "INSERT OR REPLACE INTO metadata VALUES ('experimental_scope', ?)",
        ("ca-bbox-not-state-boundary",),
    )
    db.execute(
        "INSERT OR REPLACE INTO metadata VALUES ('sampling_strategy', ?)",
        (sampling_strategy or "none",),
    )
    db.execute(
        "INSERT OR REPLACE INTO metadata VALUES ('ranking_strategy', ?)",
        (ranking_strategy,),
    )
    db.execute("INSERT OR REPLACE INTO metadata VALUES ('record_count', ?)", (str(count),))
    db.execute("INSERT OR REPLACE INTO metadata VALUES ('created_at', ?)",
               (version_timestamp(version),))

    db.execute("INSERT INTO divisions_fts(divisions_fts) VALUES('optimize')")
    db.commit()
    db.execute("VACUUM")
    db.close()
    con.close()

    return {
        "country": region_code.split("-", 1)[0],
        "region": region_code,
        "record_count": count,
        "size_bytes": output_path.stat().st_size,
        "bbox": bbox,
    }


def build_places_shards(args, version: str, version_dir: Path) -> dict:
    if not bool(getattr(args, "experimental_places_bbox_slice", False)):
        raise ValueError(
            "Places output is only a CA-bbox experiment; pass "
            "--experimental-places-bbox-slice to acknowledge it is not an exact "
            "US-CA shard and cannot be promoted"
        )
    places_subdir = version_dir / "places-experimental"
    parquet_path = getattr(
        args, "places_parquet", Path("exports/places-CA-bbox.parquet")
    )
    region_code = validate_region_code(getattr(args, "places_region", "US-CA"))
    limit: int | None = getattr(args, "places_limit", None)
    sampling_strategy: str | None = getattr(args, "places_sampling_strategy", None)
    ranking_strategy: str = getattr(args, "places_ranking_strategy", "confidence")
    if limit is not None and limit <= 0:
        raise ValueError("--places-limit must be greater than zero")
    if region_code != "US-CA":
        raise ValueError(
            "the current experimental bbox extractor is fixed to the US-CA bbox"
        )
    if limit is not None and sampling_strategy is None:
        raise ValueError(
            "--places-limit requires --places-sampling-strategy; the old composed "
            "prominence formula is a rejected experimental baseline"
        )

    # Handle S3 case: parquet_path may be string or Path that doesn't exist
    is_s3 = isinstance(parquet_path, str) and str(parquet_path).startswith("s3://")
    if not is_s3:
        pp_check = Path(parquet_path) if not isinstance(parquet_path, Path) else parquet_path
        try:
            exists = pp_check.exists()
        except Exception:
            exists = False
        if not exists:
            print(f"Places parquet not found: {parquet_path}")
            print("Generating via direct S3 read for CA bbox (may take a few minutes)...")
            release = getattr(args, "overture_release", None) or "2026-06-17.0"
            parquet_path = f"s3://overturemaps-us-west-2/release/{release}/theme=places/type=place/*"
            args.places_parquet = parquet_path
            is_s3 = True

    output_path = places_subdir / f"{PLACES_CA_BBOX_SLICE_ID}.db"
    print(f"Building experimental CA bbox Places slice from {parquet_path}")
    print("  WARNING: this is a rectangle, not an exact California state shard")
    print(f"  Stored/query ranking strategy: {ranking_strategy}")
    if ranking_strategy == "experimental-prominence":
        print("  WARNING: stored ranking uses the rejected prominence baseline")
    if limit:
        print(f"  Sampling limit: {limit:,}; strategy: {sampling_strategy}")
        if sampling_strategy == "experimental-prominence":
            print("  WARNING: experimental-prominence is a rejected baseline")

    info = build_places_shard(
        parquet_path,
        output_path,
        version,
        region_code=region_code,
        limit=limit,
        sampling_strategy=sampling_strategy,
        ranking_strategy=ranking_strategy,
    )
    size_mb = info["size_bytes"] / 1024 / 1024
    print(f"  {region_code}: {info['record_count']:,} records, {size_mb:.1f} MB")

    shard_id = PLACES_CA_BBOX_SLICE_ID
    shard_hash = hash_file(output_path)
    collection = generate_stac_collection(
        version,
        {shard_id: info},
        {shard_id: shard_hash},
        "places-experimental",
    )
    collection["id"] = f"geocoder-places-experimental-bbox-{version}"
    collection["title"] = f"Overture Places Experimental CA Bbox {version}"
    collection["description"] = (
        "Non-promotable Places experiment for the CA bounding rectangle; "
        "not an exact California shard"
    )
    collection["extent"]["spatial"]["bbox"] = [info["bbox"]]
    for link in collection["links"]:
        if link.get("rel") == "self":
            link["href"] = "./places-collection.json"
    write_json(version_dir / "places-collection.json", collection)

    return {shard_id: info}


def write_places_build_meta(
    version: str,
    version_dir: Path,
    shard_infos: dict[str, dict],
    args,
) -> Path:
    """Write shards/{version}/build-meta.json for the Places experiment."""
    overture_release = getattr(args, "overture_release", None)
    if overture_release:
        source_s3_paths = [
            f"s3://overturemaps-us-west-2/release/{overture_release}/theme=places/type=place/*"
        ]
    else:
        source_s3_paths = []

    parquet_path = getattr(
        args, "places_parquet", Path("exports/places-CA-bbox.parquet")
    )
    input_size = None
    try:
        pp = Path(parquet_path) if not isinstance(parquet_path, Path) else parquet_path
        if pp.exists():
            input_size = pp.stat().st_size
    except Exception:
        pass

    meta = {
        "version": version,
        "build_timestamp": datetime.now(timezone.utc).isoformat(),
        "overture_release": overture_release,
        "source_s3_paths": source_s3_paths,
        "division_s3_paths": [],
        "git_sha": get_git_sha(),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "duckdb_version": getattr(duckdb, "__version__", "unknown"),
        "input": {
            "parquet": str(parquet_path),
            "size_bytes": input_size,
        },
        "record_counts": {
            "total_records": sum(s["record_count"] for s in shard_infos.values()),
            "total_size_bytes": sum(s["size_bytes"] for s in shard_infos.values()),
            "shard_count": len(shard_infos),
            "shards": {sid: info["record_count"] for sid, info in shard_infos.items()},
        },
        "args": {
            "places": True,
            "experimental_places_bbox_slice": bool(
                getattr(args, "experimental_places_bbox_slice", False)
            ),
            "places_region": getattr(args, "places_region", "US-CA"),
            "places_limit": getattr(args, "places_limit", None),
            "places_sampling_strategy": getattr(
                args, "places_sampling_strategy", None
            ),
            "places_ranking_strategy": getattr(
                args, "places_ranking_strategy", "confidence"
            ),
        },
    }

    out_path = version_dir / "build-meta.json"
    write_json(out_path, meta)
    print(f"\nBuild meta: {out_path}")
    return out_path


def main():
    parser = argparse.ArgumentParser(
        description="Build the experimental (non-promotable) Places shard"
    )
    parser.add_argument("--version", help="Version string (default: date-based with suffix)")
    parser.add_argument("--version-suffix", default="0",
                        help="Version suffix (default: 0, use 1, 2, etc. for rebuilds)")
    parser.add_argument(
        "--experimental-places-bbox-slice",
        action="store_true",
        help="Acknowledge Places output is a non-promotable CA bbox experiment",
    )
    parser.add_argument("--places-region", type=str, default="US-CA",
                        help="Places region code e.g. US-CA (default: US-CA)")
    parser.add_argument("--places-parquet", type=Path, default=Path("exports/places-CA-bbox.parquet"),
                        help="Input parquet for places (flattened or raw Overture places)")
    parser.add_argument("--places-limit", type=int, default=None,
                        help="Experimental sampling limit; requires an explicit strategy")
    parser.add_argument(
        "--places-sampling-strategy",
        choices=("confidence", "experimental-prominence"),
        default=None,
        help="Explicit Places sampling order; prominence is a rejected baseline",
    )
    parser.add_argument(
        "--places-ranking-strategy",
        choices=("neutral", "confidence", "experimental-prominence"),
        default="confidence",
        help=(
            "Stored/query importance, independent of sampling; rejected prominence "
            "must be selected explicitly"
        ),
    )
    parser.add_argument("--overture-release", type=str, default=None,
                        help="Overture release tag for build metadata and places S3 fallback "
                             "(e.g., 2026-06-17.0)")
    args = parser.parse_args()

    version = args.version or get_version(args.version_suffix)
    version_dir = SHARDS_DIR / version

    shard_infos = build_places_shards(args, version, version_dir)

    try:
        write_places_build_meta(version, version_dir, shard_infos, args)
    except Exception as exc:
        print(f"Warning: failed to write build-meta.json: {exc}", file=sys.stderr)

    total_records = sum(s["record_count"] for s in shard_infos.values())
    total_size = sum(s["size_bytes"] for s in shard_infos.values())

    print("\nDone! (places-experimental geocoding)")
    print(f"  Shards: {len(shard_infos)}")
    print(f"  Total records: {total_records:,}")
    print(f"  Total size: {total_size / 1024 / 1024:.1f} MB")
    print("\nOutput:")
    print(f"  {version_dir}/places-collection.json")
    print(f"  {version_dir}/places-experimental/*.db")
    print(f"  {version_dir}/build-meta.json")


if __name__ == "__main__":
    main()
