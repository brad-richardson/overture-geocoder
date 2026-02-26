#!/usr/bin/env python3
"""Generate id-collection.json from existing R2 shards and upload it.

Discovers all id-index/*.parquet shards in R2 via glob, builds the STAC
collection metadata, and uploads it via wrangler.

Usage:
    python scripts/gen_id_collection.py --version 2026-02-26.0 --prefix-len 4
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).parent))
from stac import get_latest_release

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
    parser = argparse.ArgumentParser(description="Generate id-collection.json")
    parser.add_argument("--version", required=True)
    parser.add_argument("--prefix-len", type=int, default=4)
    parser.add_argument("--bucket", default="geocoder-shards")
    args = parser.parse_args()

    r2_config = get_r2_config()
    r2_config["bucket"] = args.bucket
    version = args.version
    bucket = r2_config["bucket"]

    print("Discovering latest Overture release...")
    release_version = get_latest_release()
    print(f"  Release: {release_version}")

    con = r2_con(r2_config)

    print("Discovering R2 shards...")
    t0 = time.time()
    glob_path = f"s3://{bucket}/{version}/id-index/*.parquet"
    rows = con.execute(f"SELECT file FROM glob('{glob_path}')").fetchall()
    shard_files = [r[0] for r in rows]
    print(f"  Found {len(shard_files)} shards in {time.time() - t0:.0f}s")

    shard_infos = {}
    for path in shard_files:
        prefix = path.rsplit("/", 1)[-1].replace(".parquet", "")
        shard_infos[prefix] = {"record_count": None, "size_bytes": None}

    now = datetime.now(timezone.utc).isoformat()
    collection = {
        "type": "Collection",
        "stac_version": "1.1.0",
        "id": f"geocoder-id-index-{version}",
        "title": f"Overture GERS ID Index {version}",
        "description": "UUID-prefix-sharded parquet index mapping GERS IDs to bounding boxes",
        "license": "CDLA-Permissive-2.0",
        "extent": {
            "spatial": {"bbox": [[-180, -90, 180, 90]]},
            "temporal": {"interval": [[now, None]]},
        },
        "summaries": {
            "shard_count": len(shard_infos),
            "total_records": 0,
            "total_size_bytes": 0,
            "prefix_len": args.prefix_len,
            "overture_release": release_version,
        },
        "items": {
            p: {"href": f"./id-index/{p}.parquet"}
            for p in sorted(shard_infos.keys())
        },
        "links": [
            {"rel": "root", "href": "../catalog.json", "type": "application/json"},
            {"rel": "parent", "href": "../catalog.json", "type": "application/json"},
            {"rel": "self", "href": "./id-collection.json", "type": "application/json"},
        ],
    }

    tmp = Path("tmp-id-collection.json")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    with open(tmp, "w") as f:
        json.dump(collection, f, indent=2)
    print(f"  Generated {tmp} ({len(shard_infos)} items)")

    # Upload via wrangler
    r2_key = f"geocoder-shards/{version}/id-collection.json"
    result = subprocess.run(
        ["wrangler", "r2", "object", "put", r2_key,
         "--file", str(tmp), "--remote"],
        capture_output=True, text=True, timeout=120,
    )
    tmp.unlink(missing_ok=True)
    if result.returncode != 0:
        print(f"  ERROR uploading: {result.stderr[:200]}")
        sys.exit(1)
    print(f"  Uploaded id-collection.json to R2 ({r2_key})")

    con.close()


if __name__ == "__main__":
    main()
