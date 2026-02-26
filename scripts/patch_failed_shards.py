#!/usr/bin/env python3
"""Patch failed ID index shards by rebuilding from R2 staging data.

Downloads relevant release data for just the target staging prefixes once,
then processes all output sub-prefixes from local data.

Usage:
    python scripts/patch_failed_shards.py --staging-prefixes 430 ff6 --version 2026-02-26.0
"""

import argparse
import os
import sys
import time
from pathlib import Path

import duckdb

# Load .env
_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    with open(_env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip())


def get_r2_config():
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID") or os.environ.get("R2_ACCOUNT_ID")
    access_key = os.environ.get("R2_ACCESS_KEY_ID")
    secret_key = os.environ.get("R2_SECRET_ACCESS_KEY")
    if not all([account_id, access_key, secret_key]):
        print("Missing R2 credentials in environment")
        sys.exit(1)
    return {
        "account_id": account_id,
        "endpoint": f"{account_id}.r2.cloudflarestorage.com",
        "key_id": access_key,
        "secret": secret_key,
        "bucket": "geocoder-shards",
    }


def r2_con(r2_config):
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute(f"""
        CREATE SECRET r2 (
            TYPE S3,
            SCOPE 's3://{r2_config["bucket"]}/',
            KEY_ID '{r2_config["key_id"]}',
            SECRET '{r2_config["secret"]}',
            ENDPOINT '{r2_config["endpoint"]}',
            REGION 'auto',
            URL_STYLE 'path'
        );
    """)
    return con


def main():
    parser = argparse.ArgumentParser(description="Patch failed ID index shards")
    parser.add_argument("--staging-prefixes", nargs="+", required=True,
                        help="3-char staging prefixes to rebuild (e.g. 430 ff6)")
    parser.add_argument("--version", required=True, help="Version string (e.g. 2026-02-26.0)")
    parser.add_argument("--bucket", default="geocoder-shards")
    args = parser.parse_args()

    r2_config = get_r2_config()
    r2_config["bucket"] = args.bucket
    version = args.version
    bucket = r2_config["bucket"]
    staging_prefixes = args.staging_prefixes

    con = r2_con(r2_config)
    con.execute("SET memory_limit = '4GB';")

    # Discover release staging files
    print("Discovering release staging files...")
    rows = con.execute(f"""
        SELECT file FROM glob('s3://{bucket}/{version}/staging/id-release-*/data.parquet')
    """).fetchall()
    release_files = sorted(r[0] for r in rows)
    print(f"  Found {len(release_files)} release files")

    # Download only the rows we need from release files (filtered by our staging prefixes)
    prefix_filter = ", ".join(f"'{sp}'" for sp in staging_prefixes)
    local_release_files = []
    for rf in release_files:
        name = rf.split("/staging/")[-1].split("/")[0]
        local_path = f"/tmp/patch-release-{name}.parquet"
        t_dl = time.time()
        try:
            con.execute(f"""
                COPY (
                    SELECT * FROM read_parquet('{rf}')
                    WHERE prefix IN ({prefix_filter})
                    ORDER BY prefix, id
                ) TO '{local_path}' (FORMAT PARQUET, COMPRESSION ZSTD);
            """)
            count = con.execute(f"SELECT COUNT(*) FROM read_parquet('{local_path}')").fetchone()[0]
            size_mb = os.path.getsize(local_path) / 1024 / 1024
            print(f"  {name}: {count:,} records, {size_mb:.1f} MB ({time.time() - t_dl:.0f}s)")
            if count > 0:
                local_release_files.append(local_path)
            else:
                os.unlink(local_path)
        except Exception as e:
            print(f"  {name}: no matching data ({e})")
    print(f"  {len(local_release_files)} release files with data for target prefixes")

    total_shards = 0
    total_records = 0
    t0 = time.time()

    for staging_prefix in staging_prefixes:
        output_prefixes = [staging_prefix]
        print(f"\nRebuilding staging prefix {staging_prefix}")

        # Download registry staging partition locally
        registry_r2 = (
            f"s3://{bucket}/{version}/staging/id-partitioned/"
            f"prefix={staging_prefix}/*.parquet"
        )
        local_registry = f"/tmp/patch-reg-{staging_prefix}.parquet"
        has_registry = False

        try:
            con.execute(f"""
                COPY (
                    SELECT id, bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax
                    FROM read_parquet('{registry_r2}')
                ) TO '{local_registry}' (FORMAT PARQUET);
            """)
            reg_count = con.execute(f"SELECT COUNT(*) FROM read_parquet('{local_registry}')").fetchone()[0]
            print(f"  Registry: {reg_count:,} records")
            has_registry = True
        except Exception as e:
            print(f"  No registry data for {staging_prefix}: {e}")

        for prefix in output_prefixes:
            id_filter = f"AND id::VARCHAR LIKE '{prefix}%'"
            sources = []

            if has_registry:
                sources.append(
                    f"SELECT id, bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax "
                    f"FROM read_parquet('{local_registry}') WHERE 1=1 {id_filter}"
                )

            # Query LOCAL release files
            for lrf in local_release_files:
                try:
                    row = con.execute(
                        f"SELECT 1 FROM read_parquet('{lrf}') "
                        f"WHERE prefix = '{staging_prefix}' {id_filter} LIMIT 1"
                    ).fetchone()
                    if row:
                        sources.append(
                            f"SELECT id, bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax "
                            f"FROM read_parquet('{lrf}') "
                            f"WHERE prefix = '{staging_prefix}' {id_filter}"
                        )
                except Exception:
                    pass

            if not sources:
                print(f"  {prefix}: no data")
                continue

            union_query = " UNION ALL ".join(sources)
            count = con.execute(f"SELECT COUNT(*) FROM ({union_query})").fetchone()[0]
            if count == 0:
                print(f"  {prefix}: 0 records")
                continue

            r2_dest = f"s3://{bucket}/{version}/id-index/{prefix}.parquet"
            con.execute(f"""
                COPY (
                    SELECT * FROM ({union_query}) ORDER BY id
                ) TO '{r2_dest}'
                (FORMAT PARQUET, COMPRESSION UNCOMPRESSED, ROW_GROUP_SIZE 100000);
            """)

            print(f"  {prefix}: {count:,} records -> R2")
            total_shards += 1
            total_records += count

        # Cleanup registry
        if has_registry:
            try:
                os.unlink(local_registry)
            except Exception:
                pass

    # Cleanup release files
    for lrf in local_release_files:
        try:
            os.unlink(lrf)
        except Exception:
            pass

    elapsed = time.time() - t0
    print(f"\nDone: {total_shards} shards, {total_records:,} records in {elapsed:.0f}s")
    con.close()


if __name__ == "__main__":
    main()
