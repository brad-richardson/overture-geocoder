#!/usr/bin/env python3
"""
Build US street index from Overture transportation segments.

Downloads all road segments via S3 sync (fast!), then filters to US,
aggregates to unique streets, and builds a compact radix trie.

Usage:
    python scripts/build_us_streets.py

Output:
    data/streets/
        raw/                      # Raw parquet files from S3 (~64 GB global)
        us-streets.parquet        # Aggregated unique US streets (~50 MB)
        us-streets.trie           # Radix trie index (~73 MB)
        us-streets.db             # SQLite index (backup)

Estimated time:
    - S3 download: 10-20 min (depending on bandwidth)
    - Processing: 5-10 min
"""

import argparse
import json
import os
import sqlite3
import struct
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import duckdb
except ImportError:
    print("Error: duckdb not installed. Run: pip install duckdb")
    sys.exit(1)

# Import from existing experiment script
sys.path.insert(0, str(Path(__file__).parent))
from stac import get_latest_release

# Output directory
OUTPUT_DIR = Path("data/streets")

# S3 bucket info
S3_BUCKET = "overturemaps-us-west-2"


def normalize_text(text: str) -> str:
    """Normalize text for search (lowercase, strip)."""
    if not text:
        return ""
    return text.lower().strip()


# ============================================================================
# Radix Trie (copied from experiment script for standalone use)
# ============================================================================

class RadixTrieNode:
    __slots__ = ['edge_label', 'children', 'values']

    def __init__(self, edge_label: str = ""):
        self.edge_label = edge_label
        self.children: dict[str, 'RadixTrieNode'] = {}
        self.values: list = []


class RadixTrie:
    def __init__(self):
        self.root = RadixTrieNode()
        self.size = 0

    def insert(self, key: str, value: any):
        if not key:
            return

        node = self.root
        i = 0

        while i < len(key):
            first_char = key[i]

            if first_char not in node.children:
                new_node = RadixTrieNode(key[i:])
                new_node.values.append(value)
                node.children[first_char] = new_node
                self.size += 1
                return

            child = node.children[first_char]
            edge = child.edge_label

            j = 0
            while j < len(edge) and i + j < len(key) and edge[j] == key[i + j]:
                j += 1

            if j == len(edge):
                i += j
                if i == len(key):
                    child.values.append(value)
                    self.size += 1
                    return
                node = child
            else:
                split_node = RadixTrieNode(edge[:j])
                child.edge_label = edge[j:]
                split_node.children[edge[j]] = child
                node.children[first_char] = split_node

                if i + j == len(key):
                    split_node.values.append(value)
                else:
                    new_leaf = RadixTrieNode(key[i + j:])
                    new_leaf.values.append(value)
                    split_node.children[key[i + j]] = new_leaf

                self.size += 1
                return

        node.values.append(value)
        self.size += 1

    def serialize(self) -> bytes:
        buffer = bytearray()
        self._serialize_node(self.root, buffer)
        return bytes(buffer)

    def _serialize_node(self, node: RadixTrieNode, buffer: bytearray):
        edge_bytes = node.edge_label.encode('utf-8')
        buffer.extend(struct.pack('<H', len(edge_bytes)))
        buffer.extend(edge_bytes)

        buffer.extend(struct.pack('<H', len(node.values)))
        for value in node.values:
            # Pack: name_len + name + city_len + city + state_len + state + lon + lat
            name = (value.get('name') or '').encode('utf-8')
            city = (value.get('city') or '').encode('utf-8')
            state = (value.get('state') or '').encode('utf-8')
            lon = value.get('lon', 0.0)
            lat = value.get('lat', 0.0)

            buffer.extend(struct.pack('<B', len(name)))
            buffer.extend(name)
            buffer.extend(struct.pack('<B', len(city)))
            buffer.extend(city)
            buffer.extend(struct.pack('<B', len(state)))
            buffer.extend(state)
            buffer.extend(struct.pack('<ff', lon, lat))

        buffer.extend(struct.pack('<B', len(node.children)))
        for char in sorted(node.children.keys()):
            buffer.extend(char.encode('utf-8')[:1])
            self._serialize_node(node.children[char], buffer)


# ============================================================================
# Main Build Functions
# ============================================================================

def get_s3_file_list(s3_path: str) -> list[tuple[str, int]]:
    """Get list of files and sizes from S3 path."""
    cmd = ["aws", "s3", "ls", s3_path, "--no-sign-request"]
    result = subprocess.run(cmd, capture_output=True, text=True)

    files = []
    for line in result.stdout.strip().split('\n'):
        if line and '.parquet' in line:
            parts = line.split()
            if len(parts) >= 4:
                size = int(parts[2])
                name = parts[3]
                files.append((name, size))
    return files


def download_via_s3_sync(release: str, output_dir: Path) -> Path:
    """Download transportation segments via aws s3 sync (fast!)."""
    print(f"\n{'='*60}")
    print("STEP 1: Downloading transportation segments via S3 sync")
    print(f"{'='*60}")

    s3_path = f"s3://{S3_BUCKET}/release/{release}/theme=transportation/type=segment/"
    local_path = output_dir / "raw"
    local_path.mkdir(parents=True, exist_ok=True)

    print(f"Source: {s3_path}")
    print(f"Destination: {local_path}")

    # Get file list first to show progress
    print("\nListing S3 files...")
    s3_files = get_s3_file_list(s3_path)
    total_size = sum(size for _, size in s3_files)
    print(f"Found {len(s3_files)} files, {total_size / 1e9:.1f} GB total")
    print("\nDownloading (this may take 10-20 minutes)...")

    start = datetime.now()
    last_update = start

    # Start sync in background and monitor progress
    cmd = [
        "aws", "s3", "sync",
        s3_path,
        str(local_path),
        "--no-sign-request",
        "--only-show-errors"
    ]

    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    # Monitor progress by checking local files
    while process.poll() is None:
        import time
        time.sleep(2)

        # Count downloaded files and size
        local_files = list(local_path.glob("*.parquet"))
        downloaded_size = sum(f.stat().st_size for f in local_files)

        # Update progress
        now = datetime.now()
        elapsed = (now - start).total_seconds()
        if elapsed > 0:
            speed = downloaded_size / elapsed / 1e6  # MB/s
            pct = (downloaded_size / total_size * 100) if total_size > 0 else 0
            remaining = (total_size - downloaded_size) / (downloaded_size / elapsed) if downloaded_size > 0 else 0

            # Clear line and print progress
            print(f"\r  Progress: {len(local_files)}/{len(s3_files)} files | "
                  f"{downloaded_size/1e9:.1f}/{total_size/1e9:.1f} GB ({pct:.0f}%) | "
                  f"{speed:.0f} MB/s | "
                  f"ETA: {remaining/60:.0f} min   ", end='', flush=True)

    print()  # New line after progress

    if process.returncode != 0:
        stderr = process.stderr.read().decode() if process.stderr else ""
        print(f"Error: S3 sync failed with code {process.returncode}")
        if stderr:
            print(f"  {stderr}")
        sys.exit(1)

    elapsed = (datetime.now() - start).total_seconds()

    # Final count
    files = list(local_path.glob("*.parquet"))
    total_size = sum(f.stat().st_size for f in files)

    print(f"\nDownload complete!")
    print(f"  Files: {len(files)}")
    print(f"  Size: {total_size / 1e9:.2f} GB")
    print(f"  Time: {elapsed/60:.1f} minutes ({total_size / 1e6 / elapsed:.0f} MB/s)")

    return local_path


def filter_us_streets(raw_dir: Path, output_path: Path) -> int:
    """Filter to US streets and extract relevant columns."""
    print(f"\n{'='*60}")
    print("STEP 2: Filtering to US streets")
    print(f"{'='*60}")

    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    con.execute("SET memory_limit = '8GB';")
    con.execute("SET threads = 4;")

    # Query local parquet files, filter to continental US bbox
    query = f"""
        COPY (
            SELECT
                names.primary as street_name,
                class as road_class,
                ST_X(ST_Centroid(geometry)) as lon,
                ST_Y(ST_Centroid(geometry)) as lat
            FROM read_parquet('{raw_dir}/*.parquet')
            WHERE names.primary IS NOT NULL
              AND subtype = 'road'
              AND bbox.xmin >= -125 AND bbox.xmax <= -66
              AND bbox.ymin >= 24 AND bbox.ymax <= 50
        ) TO '{output_path}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """

    print("Filtering to continental US...")
    start = datetime.now()
    con.execute(query)
    elapsed = (datetime.now() - start).total_seconds()

    count = con.execute(f"SELECT COUNT(*) FROM read_parquet('{output_path}')").fetchone()[0]
    size = output_path.stat().st_size

    con.close()

    print(f"\nFiltering complete!")
    print(f"  US road segments: {count:,}")
    print(f"  Size: {size / 1e9:.2f} GB")
    print(f"  Time: {elapsed:.0f} seconds")

    return count


def aggregate_streets(input_path: Path, output_path: Path) -> int:
    """Aggregate segments to unique streets."""
    print(f"\n{'='*60}")
    print("STEP 3: Aggregating to unique streets")
    print(f"{'='*60}")

    con = duckdb.connect()

    # Road class priority for ranking
    query = f"""
        COPY (
            SELECT
                street_name,
                LOWER(street_name) as street_lower,
                -- Pick best road class (motorway > trunk > primary > ...)
                FIRST(road_class ORDER BY CASE road_class
                    WHEN 'motorway' THEN 1
                    WHEN 'trunk' THEN 2
                    WHEN 'primary' THEN 3
                    WHEN 'secondary' THEN 4
                    WHEN 'tertiary' THEN 5
                    WHEN 'residential' THEN 6
                    ELSE 7
                END) as road_class,
                COUNT(*) as segment_count,
                AVG(lon) as lon,
                AVG(lat) as lat
            FROM read_parquet('{input_path}')
            WHERE street_name IS NOT NULL
            GROUP BY street_name, LOWER(street_name)
        ) TO '{output_path}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """

    print("Aggregating...")
    start = datetime.now()
    con.execute(query)
    elapsed = (datetime.now() - start).total_seconds()

    count = con.execute(f"SELECT COUNT(*) FROM read_parquet('{output_path}')").fetchone()[0]
    size = output_path.stat().st_size

    con.close()

    print(f"\nAggregation complete!")
    print(f"  Unique streets: {count:,}")
    print(f"  Size: {size / 1e6:.2f} MB")
    print(f"  Time: {elapsed:.1f} seconds")

    return count


def build_trie(input_path: Path, output_path: Path) -> int:
    """Build radix trie from aggregated streets."""
    print(f"\n{'='*60}")
    print("STEP 4: Building radix trie")
    print(f"{'='*60}")

    con = duckdb.connect()
    rows = con.execute(f"""
        SELECT street_name, street_lower, road_class, lon, lat
        FROM read_parquet('{input_path}')
    """).fetchall()
    con.close()

    print(f"Building trie from {len(rows):,} streets...")
    start = datetime.now()

    trie = RadixTrie()
    for street_name, street_lower, road_class, lon, lat in rows:
        if street_lower:
            value = {
                'name': street_name,
                'city': '',  # Not available from transportation
                'state': '',
                'lon': lon,
                'lat': lat,
            }
            trie.insert(street_lower, value)

    # Serialize
    data = trie.serialize()
    with open(output_path, 'wb') as f:
        f.write(data)

    elapsed = (datetime.now() - start).total_seconds()
    size = output_path.stat().st_size

    print(f"\nTrie complete!")
    print(f"  Entries: {trie.size:,}")
    print(f"  Size: {size / 1e6:.2f} MB")
    print(f"  Time: {elapsed:.1f} seconds")

    return trie.size


def build_sqlite(input_path: Path, output_path: Path) -> int:
    """Build SQLite index as backup."""
    print(f"\n{'='*60}")
    print("STEP 5: Building SQLite index (backup)")
    print(f"{'='*60}")

    con = duckdb.connect()
    rows = con.execute(f"""
        SELECT street_name, street_lower, road_class, segment_count, lon, lat
        FROM read_parquet('{input_path}')
    """).fetchall()
    con.close()

    if output_path.exists():
        output_path.unlink()

    db = sqlite3.connect(output_path)
    db.executescript("""
        PRAGMA journal_mode=DELETE;
        PRAGMA synchronous=NORMAL;

        CREATE TABLE streets (
            rowid INTEGER PRIMARY KEY,
            street_name TEXT NOT NULL,
            street_lower TEXT NOT NULL,
            road_class TEXT,
            segment_count INTEGER,
            lon REAL NOT NULL,
            lat REAL NOT NULL
        );

        CREATE INDEX idx_street_lower ON streets(street_lower);
    """)

    print(f"Inserting {len(rows):,} streets...")
    start = datetime.now()

    db.executemany("""
        INSERT INTO streets (street_name, street_lower, road_class, segment_count, lon, lat)
        VALUES (?, ?, ?, ?, ?, ?)
    """, rows)

    db.commit()
    db.execute("VACUUM")
    db.close()

    elapsed = (datetime.now() - start).total_seconds()
    size = output_path.stat().st_size

    print(f"\nSQLite complete!")
    print(f"  Size: {size / 1e6:.2f} MB")
    print(f"  Time: {elapsed:.1f} seconds")

    return len(rows)


def main():
    parser = argparse.ArgumentParser(description="Build US street index")
    parser.add_argument("--release", help="Overture release version (default: latest)")
    parser.add_argument("--skip-download", action="store_true",
                       help="Skip S3 download if raw parquet files already exist")
    parser.add_argument("--skip-filter", action="store_true",
                       help="Skip US filtering if filtered parquet already exists")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR,
                       help=f"Output directory (default: {OUTPUT_DIR})")
    parser.add_argument("--keep-raw", action="store_true",
                       help="Keep raw parquet files after processing (default: delete to save space)")
    args = parser.parse_args()

    # Get release
    release = args.release
    if not release:
        print("Fetching latest Overture release...")
        release = get_latest_release()
    print(f"Using Overture release: {release}")

    # Setup paths
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    raw_dir = output_dir / "raw"
    us_segments_path = output_dir / "us-segments.parquet"
    aggregated_path = output_dir / "us-streets.parquet"
    trie_path = output_dir / "us-streets.trie"
    sqlite_path = output_dir / "us-streets.db"

    # Track metrics
    metrics = {
        "release": release,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    # Step 1: Download via S3 sync
    if args.skip_download and raw_dir.exists() and list(raw_dir.glob("*.parquet")):
        print(f"\nSkipping download, using existing: {raw_dir}")
        files = list(raw_dir.glob("*.parquet"))
        print(f"  Found {len(files)} parquet files")
    else:
        download_via_s3_sync(release, output_dir)

    # Step 2: Filter to US
    if args.skip_filter and us_segments_path.exists():
        print(f"\nSkipping filter, using existing: {us_segments_path}")
        con = duckdb.connect()
        metrics["us_segments"] = con.execute(
            f"SELECT COUNT(*) FROM read_parquet('{us_segments_path}')"
        ).fetchone()[0]
        con.close()
    else:
        metrics["us_segments"] = filter_us_streets(raw_dir, us_segments_path)

    # Step 3: Aggregate to unique streets
    metrics["unique_streets"] = aggregate_streets(us_segments_path, aggregated_path)

    # Step 4: Build trie
    metrics["trie_entries"] = build_trie(aggregated_path, trie_path)
    metrics["trie_size"] = trie_path.stat().st_size

    # Step 5: Build SQLite
    build_sqlite(aggregated_path, sqlite_path)
    metrics["sqlite_size"] = sqlite_path.stat().st_size

    # Clean up raw files to save space (unless --keep-raw)
    if not args.keep_raw and raw_dir.exists():
        print(f"\nCleaning up raw parquet files to save space...")
        import shutil
        shutil.rmtree(raw_dir)
        print(f"  Deleted {raw_dir}")

    # Save metrics
    metrics_path = output_dir / "metrics.json"
    with open(metrics_path, "w") as f:
        json.dump(metrics, f, indent=2)

    # Summary
    print(f"\n{'='*60}")
    print("BUILD COMPLETE!")
    print(f"{'='*60}")
    print(f"\nOutput files:")
    if us_segments_path.exists():
        print(f"  {us_segments_path} ({us_segments_path.stat().st_size / 1e9:.2f} GB)")
    print(f"  {aggregated_path} ({aggregated_path.stat().st_size / 1e6:.2f} MB)")
    print(f"  {trie_path} ({trie_path.stat().st_size / 1e6:.2f} MB)")
    print(f"  {sqlite_path} ({sqlite_path.stat().st_size / 1e6:.2f} MB)")
    print(f"\nMetrics: {metrics_path}")
    print(f"\nUS road segments: {metrics.get('us_segments', 'N/A'):,}")
    print(f"Unique streets: {metrics['unique_streets']:,}")
    print(f"Trie size: {metrics['trie_size'] / 1e6:.2f} MB")


if __name__ == "__main__":
    main()
