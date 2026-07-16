#!/usr/bin/env python3
"""Generate id-collection.json from existing R2 shards and upload it.

Discovers all id-index/*.parquet shards in R2 via glob, builds the STAC
collection metadata, and uploads it via wrangler.

Usage:
    python scripts/gen_id_collection.py --version 2026-02-26.0 --prefix-len 3
"""

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).parent))
from build_id_index import (
    _classify_shard_set,
    _format_metadata,
)
from common import write_json
from id_index_protocol import (
    _load_locator_dictionary_binding,
    _validate_build_marker_dictionary_sha,
)
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
    parser.add_argument("--prefix-len", type=int, default=3)
    parser.add_argument("--bucket", default="geocoder-shards")
    parser.add_argument("--release",
                        help="Overture release version (default: discover latest from STAC)")
    args = parser.parse_args()

    r2_config = get_r2_config()
    r2_config["bucket"] = args.bucket
    version = args.version
    bucket = r2_config["bucket"]

    if args.release:
        release_version = args.release
        print(f"Using provided Overture release: {release_version}")
    else:
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

    # Shared validator checks exact order, physical types, UUID length, and
    # uniform format for every footer before either metadata object is written.
    format_version = _classify_shard_set(con, shard_files)
    dictionary_reference = None
    if format_version >= 3:
        # Load the manifest-bound dictionary reference and its input inventory
        # set SHA through the protocol module, then require every build marker
        # to match both — the same fail-closed pair phase_metadata enforces.
        dictionary_reference, input_inventory_set_sha256 = (
            _load_locator_dictionary_binding(r2_config, version, release_version)
        )
        _validate_build_marker_dictionary_sha(
            r2_config,
            version,
            dictionary_reference["sha256"],
            input_inventory_set_sha256,
        )
    format_metadata = _format_metadata(
        format_version, release_version, dictionary_reference)

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
            **format_metadata,
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
    write_json(tmp, collection)
    print(f"  Generated {tmp} ({len(shard_infos)} items)")

    # Upload via wrangler
    r2_key = f"{bucket}/{version}/id-collection.json"
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

    # Upload id-meta.json (tiny metadata for fast worker prefix_len lookup)
    meta = {
        "prefix_len": args.prefix_len,
        "shard_count": len(shard_infos),
        **format_metadata,
    }
    tmp_meta = Path("tmp-id-meta.json")
    write_json(tmp_meta, meta)
    meta_key = f"{bucket}/{version}/id-meta.json"
    result = subprocess.run(
        ["wrangler", "r2", "object", "put", meta_key,
         "--file", str(tmp_meta), "--remote"],
        capture_output=True, text=True, timeout=120,
    )
    tmp_meta.unlink(missing_ok=True)
    if result.returncode != 0:
        print(f"  ERROR uploading: {result.stderr[:200]}")
        sys.exit(1)
    print(f"  Uploaded id-meta.json to R2 ({meta_key})")

    con.close()


if __name__ == "__main__":
    main()
