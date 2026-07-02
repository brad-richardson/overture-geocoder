#!/usr/bin/env python3
"""Row-group size experiment for /id cold-read latency (perf plan, proposal 3).

Rebuilds a few real id-index shards at different ROW_GROUP_SIZE values,
uploads them to a scratch prefix (`rg-experiment/`), and measures the cold
lookup path exactly the way the worker pays for it:

    1. ranged GET of the footer suffix (FOOTER_SUFFIX_SIZE = 32 KB)
    2. ranged GET of the one row group covering the target ID

No staging data needed — inputs are the live version's own shards — so this
can run any time. Cleans up its scratch uploads afterwards unless
--keep-uploads is passed.

Usage:
    python scripts/rowgroup_experiment.py --version 2026-07-02.2 \
        [--prefixes 0a1,7f2,e3c] [--sizes 25000,50000,100000] [--lookups 8]

Requires R2 creds in the environment (R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY,
CLOUDFLARE_ACCOUNT_ID) and `pip install duckdb boto3`.
"""

import argparse
import json
import os
import random
import statistics
import struct
import sys
import tempfile
import time
from pathlib import Path

import boto3
import duckdb

# Mirrors FOOTER_SUFFIX_SIZE in crates/geocoder-worker/src/stac.rs
FOOTER_SUFFIX_SIZE = 32 * 1024
SCRATCH_PREFIX = "rg-experiment"

# Deterministic spread across the shard space; override with --prefixes
DEFAULT_PREFIXES = ["0a1", "7f2", "e3c"]
DEFAULT_SIZES = [25_000, 50_000, 100_000]


def r2_client():
    account = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    key = os.environ.get("R2_ACCESS_KEY_ID")
    secret = os.environ.get("R2_SECRET_ACCESS_KEY")
    if not (account and key and secret):
        sys.exit("R2 credentials required: CLOUDFLARE_ACCOUNT_ID, "
                 "R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY")
    return boto3.client(
        "s3",
        endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
        aws_access_key_id=key,
        aws_secret_access_key=secret,
        region_name="auto",
    )


def ranged_get(s3, bucket, key, start=None, end=None, suffix=None):
    """Timed ranged GET. Returns (bytes, seconds)."""
    if suffix is not None:
        rng = f"bytes=-{suffix}"
    else:
        rng = f"bytes={start}-{end - 1}"
    t0 = time.perf_counter()
    body = s3.get_object(Bucket=bucket, Key=key, Range=rng)["Body"].read()
    return body, time.perf_counter() - t0


def footer_length(tail: bytes) -> int:
    """Parquet footer metadata length from the file's last 8 bytes."""
    assert tail[-4:] == b"PAR1", "not a parquet file"
    return struct.unpack("<I", tail[-8:-4])[0]


def row_group_spans(local_path):
    """(start_offset, end_offset, num_rows) per row group, via DuckDB."""
    con = duckdb.connect()
    rows = con.execute(f"""
        SELECT row_group_id,
               MIN(LEAST(coalesce(dictionary_page_offset, data_page_offset),
                          data_page_offset)) AS start_off,
               MAX(data_page_offset + total_compressed_size) AS end_off,
               MAX(row_group_num_rows) AS num_rows
        FROM parquet_metadata('{local_path}')
        GROUP BY row_group_id ORDER BY row_group_id
    """).fetchall()
    con.close()
    return [(int(r[1]), int(r[2]), int(r[3])) for r in rows]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--version", required=True,
                    help="Live version whose id-index shards to sample")
    ap.add_argument("--bucket", default="geocoder-shards")
    ap.add_argument("--prefixes", default=",".join(DEFAULT_PREFIXES))
    ap.add_argument("--sizes", default=",".join(str(s) for s in DEFAULT_SIZES))
    ap.add_argument("--lookups", type=int, default=8,
                    help="Timed cold lookups per (prefix, size)")
    ap.add_argument("--keep-uploads", action="store_true")
    ap.add_argument("--output", help="Write raw JSON results here")
    args = ap.parse_args()

    prefixes = [p.strip() for p in args.prefixes.split(",") if p.strip()]
    sizes = [int(s) for s in args.sizes.split(",")]
    s3 = r2_client()
    con = duckdb.connect()
    workdir = Path(tempfile.mkdtemp(prefix="rg-exp-"))
    results = []
    uploaded = []

    for prefix in prefixes:
        src_key = f"{args.version}/id-index/{prefix}.parquet"
        local_src = workdir / f"{prefix}.parquet"
        print(f"\n=== {prefix} ===")
        s3.download_file(args.bucket, src_key, str(local_src))
        n_rows = con.execute(
            f"SELECT COUNT(*) FROM read_parquet('{local_src}')").fetchone()[0]
        print(f"  source: {local_src.stat().st_size / 1e6:.1f} MB, {n_rows:,} rows")

        # IDs to look up (same set across sizes for comparability)
        random.seed(int(prefix, 16))
        sample_rows = con.execute(f"""
            SELECT id::VARCHAR FROM read_parquet('{local_src}')
            USING SAMPLE {args.lookups} ROWS (reservoir, {int(prefix, 16)})
        """).fetchall()
        lookup_ids = [r[0] for r in sample_rows]

        for size in sizes:
            local_out = workdir / f"{prefix}-rg{size}.parquet"
            con.execute(f"""
                COPY (SELECT * FROM read_parquet('{local_src}') ORDER BY id)
                TO '{local_out}'
                (FORMAT PARQUET, COMPRESSION UNCOMPRESSED, ROW_GROUP_SIZE {size});
            """)
            dest_key = f"{SCRATCH_PREFIX}/rg{size}/{prefix}.parquet"
            s3.upload_file(str(local_out), args.bucket, dest_key)
            uploaded.append(dest_key)

            spans = row_group_spans(local_out)
            tail = local_out.read_bytes()[-8:]
            flen = footer_length(tail)
            rg_bytes = [e - s for s, e, _ in spans]

            # Timed cold lookups against R2
            suffix_times, rg_times, read_bytes = [], [], []
            extra_footer_reads = 0
            for lid in lookup_ids:
                body, t_suffix = ranged_get(
                    s3, args.bucket, dest_key, suffix=FOOTER_SUFFIX_SIZE)
                suffix_times.append(t_suffix)
                if flen + 8 > FOOTER_SUFFIX_SIZE:
                    extra_footer_reads += 1  # worker would pay a second read
                # Locate the row group for this id (sorted file: nth row)
                row_n = con.execute(f"""
                    SELECT COUNT(*) FROM read_parquet('{local_out}')
                    WHERE id::VARCHAR < '{lid}'
                """).fetchone()[0]
                acc = 0
                for s, e, nr in spans:
                    if row_n < acc + nr:
                        _, t_rg = ranged_get(s3, args.bucket, dest_key, s, e)
                        rg_times.append(t_rg)
                        read_bytes.append((e - s) + FOOTER_SUFFIX_SIZE)
                        break
                    acc += nr

            rec = {
                "prefix": prefix, "row_group_size": size,
                "rows": n_rows, "row_groups": len(spans),
                "footer_bytes": flen,
                "footer_fits_32k": flen + 8 <= FOOTER_SUFFIX_SIZE,
                "mean_rowgroup_bytes": int(statistics.mean(rg_bytes)),
                "cold_read_bytes_p50": int(statistics.median(read_bytes)),
                "suffix_ms_p50": round(statistics.median(suffix_times) * 1000, 1),
                "rowgroup_ms_p50": round(statistics.median(rg_times) * 1000, 1),
                "total_ms_p50": round(
                    (statistics.median(suffix_times)
                     + statistics.median(rg_times)) * 1000, 1),
            }
            results.append(rec)
            fits = "ok" if rec["footer_fits_32k"] else "OVER 32K!"
            print(f"  rg={size:>6}: {rec['row_groups']:>3} groups, "
                  f"footer {flen / 1024:.1f} KB ({fits}), "
                  f"rowgroup ~{rec['mean_rowgroup_bytes'] / 1e6:.2f} MB, "
                  f"cold read p50 {rec['cold_read_bytes_p50'] / 1e6:.2f} MB / "
                  f"{rec['total_ms_p50']} ms")

    print("\n=== Summary (median across prefixes) ===")
    print(f"{'rg size':>8} {'groups':>7} {'footer KB':>10} "
          f"{'cold MB':>8} {'cold ms':>8}")
    for size in sizes:
        rs = [r for r in results if r["row_group_size"] == size]
        print(f"{size:>8} "
              f"{statistics.median(r['row_groups'] for r in rs):>7.0f} "
              f"{statistics.median(r['footer_bytes'] for r in rs) / 1024:>10.1f} "
              f"{statistics.median(r['cold_read_bytes_p50'] for r in rs) / 1e6:>8.2f} "
              f"{statistics.median(r['total_ms_p50'] for r in rs):>8.1f}")

    if args.output:
        Path(args.output).write_text(json.dumps(
            {"version": args.version, "lookups": args.lookups,
             "footer_suffix_bytes": FOOTER_SUFFIX_SIZE, "results": results},
            indent=1))
        print(f"\nWrote {args.output}")

    if not args.keep_uploads:
        print(f"Cleaning up {len(uploaded)} scratch uploads...")
        for i in range(0, len(uploaded), 1000):
            s3.delete_objects(
                Bucket=args.bucket,
                Delete={"Objects": [{"Key": k} for k in uploaded[i:i + 1000]],
                        "Quiet": True})
    con.close()


if __name__ == "__main__":
    main()
