#!/usr/bin/env python3
"""Patch failed ID index shards by rebuilding from R2 staging data.

Downloads relevant release data for just the target staging prefixes once,
then processes all output sub-prefixes from local data.

Usage:
    python scripts/patch_failed_shards.py --staging-prefixes 430 ff6 \
        --version 2026-07-02.3 --release 2026-06-17.0
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).parent))
from build_id_index import (
    ROW_GROUP_SIZE,
    _assert_compact_locator_mapping,
    _assert_locator_rows,
    _assert_shard_schema,
    _compact_locator_query,
    _glob_files,
    _load_locator_manifest_and_dictionary,
    _write_local_dictionary_tables,
)

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


def _discover_release_staging_files(con, bucket, version):
    """Return staged release files; propagate every non-empty-source error."""
    files = sorted(set(
        _glob_files(
            con,
            f"s3://{bucket}/{version}/staging/id-release-*/bucket=*/*.parquet",
        )
        + _glob_files(
            con,
            f"s3://{bucket}/{version}/staging/id-release-*/data.parquet",
        )
    ))
    if not files:
        raise RuntimeError(
            f"No release staging files found for {version}; refusing partial patch")
    return files


def _local_release_path(remote_path, file_index, pid=None):
    """Return a collision-free temp path for one staged release object."""
    name = remote_path.split("/staging/")[-1].split("/")[0]
    return f"/tmp/patch-release-{name}-{file_index}-{pid or os.getpid()}.parquet"


def _assert_target_version_unpublished(con, bucket, version, force_unsafe):
    """Refuse to overwrite shards in an already-published, immutable version.

    A catalogued version — or one that already carries a release manifest —
    is live and immutable: patching it would silently mutate data the catalog
    guarantees. Fail closed unless the operator explicitly passes
    --force-unsafe.
    """
    reason = None
    if _glob_files(con, f"s3://{bucket}/{version}/release-manifest.json"):
        reason = f"a release manifest exists at {version}/release-manifest.json"
    elif _glob_files(con, f"s3://{bucket}/catalog.json"):
        raw = con.execute(
            f"SELECT content FROM read_text('s3://{bucket}/catalog.json')"
        ).fetchone()[0]
        try:
            catalog = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Unreadable catalog.json in {bucket}: {exc}") from exc
        for link in catalog.get("links", []):
            if not isinstance(link, dict) or link.get("rel") != "child":
                continue
            href = link.get("href")
            if isinstance(href, str) and href.strip("./").split("/", 1)[0] == version:
                reason = f"catalog.json references version {version}"
                break
    if reason is None:
        return
    if not force_unsafe:
        print(
            f"Refusing to patch published version {version}: {reason}. "
            "Published versions are immutable; build a new version instead. "
            "Pass --force-unsafe to override (this mutates live data)."
        )
        sys.exit(1)
    print(
        "WARNING: --force-unsafe set; overwriting shards in published version "
        f"{version} ({reason}). This mutates immutable, live-catalogued data "
        "and can corrupt readers that rely on the release manifest."
    )


def main():
    parser = argparse.ArgumentParser(description="Patch failed ID index shards")
    parser.add_argument("--staging-prefixes", nargs="+", required=True,
                        help="3-char staging prefixes to rebuild (e.g. 430 ff6)")
    parser.add_argument("--version", required=True, help="Version string (e.g. 2026-02-26.0)")
    parser.add_argument("--release", required=True,
                        help="Pinned Overture release represented by the staging data")
    parser.add_argument("--bucket", default="geocoder-shards")
    parser.add_argument(
        "--force-unsafe", action="store_true",
        help="Overwrite shards even in a published/catalogued version "
             "(mutates immutable live data)")
    args = parser.parse_args()

    r2_config = get_r2_config()
    r2_config["bucket"] = args.bucket
    version = args.version
    bucket = r2_config["bucket"]
    staging_prefixes = args.staging_prefixes

    con = r2_con(r2_config)
    con.execute("SET memory_limit = '4GB';")

    # Fail closed before any write if this version is already published.
    _assert_target_version_unpublished(con, bucket, version, args.force_unsafe)

    # Patch builds must reuse the exact immutable global dictionary. A subset
    # is never allowed to renumber IDs or introduce unseen tuples/releases.
    _, dictionary, _ = _load_locator_manifest_and_dictionary(
        r2_config, version, args.release)
    source_dictionary_path, release_dictionary_path = (
        _write_local_dictionary_tables(dictionary))

    # Discover release staging files
    print("Discovering release staging files...")
    release_files = _discover_release_staging_files(con, bucket, version)
    print(f"  Found {len(release_files)} release files")

    # Download only the rows we need from release files (filtered by our staging prefixes)
    prefix_filter = ", ".join(f"'{sp}'" for sp in staging_prefixes)
    local_release_files = []
    for file_index, rf in enumerate(release_files):
        name = rf.split("/staging/")[-1].split("/")[0]
        # Bucketed staging contains many objects under the same type directory.
        # Keep each download distinct: reusing only `name` overwrote earlier
        # buckets and could later unlink a path already queued for the merge.
        local_path = _local_release_path(rf, file_index)
        t_dl = time.time()
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
        registry_files = _glob_files(con, registry_r2)
        has_registry = bool(registry_files)

        if has_registry:
            con.execute(f"""
                COPY (
                    SELECT id, bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax,
                           feature_type, filename, last_seen_release,
                           registry_member, source_theme
                    FROM read_parquet('{registry_r2}')
                ) TO '{local_registry}' (FORMAT PARQUET);
            """)
            reg_count = con.execute(f"SELECT COUNT(*) FROM read_parquet('{local_registry}')").fetchone()[0]
            print(f"  Registry: {reg_count:,} records")
        else:
            print(f"  No registry data for {staging_prefix}")

        for prefix in output_prefixes:
            id_filter = f"AND id::VARCHAR LIKE '{prefix}%'"
            sources = []

            if has_registry:
                sources.append(
                    f"SELECT id, bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax, "
                    f"feature_type, filename, last_seen_release, registry_member, "
                    f"source_theme "
                    f"FROM read_parquet('{local_registry}') WHERE 1=1 {id_filter}"
                )

            # Query LOCAL release files
            for lrf in local_release_files:
                row = con.execute(
                    f"SELECT 1 FROM read_parquet('{lrf}') "
                    f"WHERE prefix = '{staging_prefix}' {id_filter} LIMIT 1"
                ).fetchone()
                if row:
                    sources.append(
                        f"SELECT id, bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax, "
                        f"feature_type, filename, last_seen_release, registry_member, "
                        f"source_theme "
                        f"FROM read_parquet('{lrf}') "
                        f"WHERE prefix = '{staging_prefix}' {id_filter}"
                    )

            if not sources:
                print(f"  {prefix}: no data")
                continue

            union_query = " UNION ALL ".join(sources)
            _assert_locator_rows(con, union_query, prefix, args.release)
            mapped_query = _compact_locator_query(
                union_query, source_dictionary_path, release_dictionary_path)
            _assert_compact_locator_mapping(con, mapped_query, prefix)
            count = con.execute(f"SELECT COUNT(*) FROM ({union_query})").fetchone()[0]
            if count == 0:
                print(f"  {prefix}: 0 records")
                continue

            r2_dest = f"s3://{bucket}/{version}/id-index/{prefix}.parquet"
            con.execute(f"""
                COPY (
                    SELECT id, bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax,
                           source_file_id, last_seen_release_id,
                           registry_member
                    FROM ({mapped_query}) ORDER BY id
                ) TO '{r2_dest}'
                (FORMAT PARQUET, COMPRESSION UNCOMPRESSED,
                 ROW_GROUP_SIZE {int(ROW_GROUP_SIZE)});
            """)

            # Verify footer (worker reads columns positionally)
            _assert_shard_schema(con, r2_dest)

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
    for lrf in local_release_files + [source_dictionary_path, release_dictionary_path]:
        try:
            os.unlink(lrf)
        except Exception:
            pass

    elapsed = time.time() - t0
    print(f"\nDone: {total_shards} shards, {total_records:,} records in {elapsed:.0f}s")
    con.close()


if __name__ == "__main__":
    main()
