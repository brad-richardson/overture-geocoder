#!/usr/bin/env python3
"""
Experimental script for places/addresses sharding strategies.

Tests two sharding approaches for Overture places and addresses data:
1. Region-level: All places for a region in one shard (like current divisions)
2. Tiered: Lightweight index shard + detailed data shard

Usage:
    # Tiny test (downtown SF block) - dry run first
    python scripts/experiment_places_addresses.py --bbox "-122.42,37.78,-122.41,37.79" --dry-run

    # Tiny test (actually run)
    python scripts/experiment_places_addresses.py --bbox "-122.42,37.78,-122.41,37.79"

    # Single city (San Francisco)
    python scripts/experiment_places_addresses.py --bbox "-122.52,37.70,-122.35,37.82"

    # Full state (CA) - run on larger machine with disk space
    python scripts/experiment_places_addresses.py --region US-CA --output-dir /data/experiment

    # Addresses only
    python scripts/experiment_places_addresses.py --bbox "-122.42,37.78,-122.41,37.79" --no-places

Output:
    exports/experiment/
        places-raw.parquet          # Raw places data
        addresses-raw.parquet       # Raw addresses data
        region/
            places-US-CA.db         # Region-level shard (all data)
            addresses-US-CA.db
        tiered/
            places-index-US-CA.db   # Lightweight index (name, category, coords)
            places-detail-US-CA.db  # Full data for display
            addresses-index-US-CA.db
            addresses-detail-US-CA.db
        metrics.json                # Size/count analysis
"""

import argparse
import json
import os
import sqlite3
import struct
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    import duckdb
except ImportError:
    print("Error: duckdb not installed. Run: pip install duckdb")
    sys.exit(1)

try:
    import msgpack
except ImportError:
    msgpack = None  # Optional, will fall back to JSON

# Import STAC helper for getting latest release
sys.path.insert(0, str(Path(__file__).parent))
from stac import get_latest_release

# Default output directory
DEFAULT_OUTPUT_DIR = Path("exports/experiment")

# US state bounding boxes for --region support
# Source: https://www.census.gov/geographies/mapping-files/time-series/geo/carto-boundary-file.html
US_STATE_BBOXES = {
    "US-AL": (-88.47, 30.22, -84.89, 35.01),  # Alabama
    "US-AK": (-179.15, 51.21, 179.77, 71.35),  # Alaska
    "US-AZ": (-114.81, 31.33, -109.05, 37.00),  # Arizona
    "US-AR": (-94.62, 33.00, -89.64, 36.50),  # Arkansas
    "US-CA": (-124.41, 32.53, -114.13, 42.01),  # California
    "US-CO": (-109.06, 36.99, -102.04, 41.00),  # Colorado
    "US-CT": (-73.73, 40.98, -71.79, 42.05),  # Connecticut
    "US-DE": (-75.79, 38.45, -75.05, 39.84),  # Delaware
    "US-FL": (-87.63, 24.52, -80.03, 31.00),  # Florida
    "US-GA": (-85.61, 30.36, -80.84, 35.00),  # Georgia
    "US-HI": (-160.25, 18.91, -154.81, 22.24),  # Hawaii
    "US-ID": (-117.24, 41.99, -111.04, 49.00),  # Idaho
    "US-IL": (-91.51, 36.97, -87.50, 42.51),  # Illinois
    "US-IN": (-88.10, 37.77, -84.78, 41.76),  # Indiana
    "US-IA": (-96.64, 40.38, -90.14, 43.50),  # Iowa
    "US-KS": (-102.05, 36.99, -94.59, 40.00),  # Kansas
    "US-KY": (-89.57, 36.50, -81.96, 39.15),  # Kentucky
    "US-LA": (-94.04, 28.93, -89.00, 33.02),  # Louisiana
    "US-ME": (-71.08, 43.06, -66.95, 47.46),  # Maine
    "US-MD": (-79.49, 37.91, -75.05, 39.72),  # Maryland
    "US-MA": (-73.51, 41.24, -69.93, 42.89),  # Massachusetts
    "US-MI": (-90.42, 41.70, -82.42, 48.19),  # Michigan
    "US-MN": (-97.24, 43.50, -89.49, 49.38),  # Minnesota
    "US-MS": (-91.66, 30.17, -88.10, 35.00),  # Mississippi
    "US-MO": (-95.77, 35.99, -89.10, 40.61),  # Missouri
    "US-MT": (-116.05, 44.36, -104.04, 49.00),  # Montana
    "US-NE": (-104.05, 40.00, -95.31, 43.00),  # Nebraska
    "US-NV": (-120.00, 35.00, -114.04, 42.00),  # Nevada
    "US-NH": (-72.56, 42.70, -70.70, 45.31),  # New Hampshire
    "US-NJ": (-75.56, 38.93, -73.89, 41.36),  # New Jersey
    "US-NM": (-109.05, 31.33, -103.00, 37.00),  # New Mexico
    "US-NY": (-79.76, 40.50, -71.86, 45.02),  # New York
    "US-NC": (-84.32, 33.84, -75.46, 36.59),  # North Carolina
    "US-ND": (-104.05, 45.93, -96.55, 49.00),  # North Dakota
    "US-OH": (-84.82, 38.40, -80.52, 42.33),  # Ohio
    "US-OK": (-103.00, 33.62, -94.43, 37.00),  # Oklahoma
    "US-OR": (-124.57, 41.99, -116.46, 46.29),  # Oregon
    "US-PA": (-80.52, 39.72, -74.69, 42.27),  # Pennsylvania
    "US-RI": (-71.91, 41.15, -71.12, 42.02),  # Rhode Island
    "US-SC": (-83.35, 32.03, -78.54, 35.22),  # South Carolina
    "US-SD": (-104.06, 42.48, -96.44, 45.95),  # South Dakota
    "US-TN": (-90.31, 34.98, -81.65, 36.68),  # Tennessee
    "US-TX": (-106.65, 25.84, -93.51, 36.50),  # Texas
    "US-UT": (-114.05, 37.00, -109.04, 42.00),  # Utah
    "US-VT": (-73.44, 42.73, -71.46, 45.02),  # Vermont
    "US-VA": (-83.68, 36.54, -75.24, 39.47),  # Virginia
    "US-WA": (-124.85, 45.54, -116.92, 49.00),  # Washington
    "US-WV": (-82.64, 37.20, -77.72, 40.64),  # West Virginia
    "US-WI": (-92.89, 42.49, -86.25, 47.08),  # Wisconsin
    "US-WY": (-111.06, 40.99, -104.05, 45.01),  # Wyoming
    "US-DC": (-77.12, 38.79, -76.91, 38.99),  # District of Columbia
}


def bbox_to_wkt(bbox: tuple[float, float, float, float]) -> str:
    """Convert bbox (min_lon, min_lat, max_lon, max_lat) to WKT polygon."""
    min_lon, min_lat, max_lon, max_lat = bbox
    return f"POLYGON(({min_lon} {min_lat}, {max_lon} {min_lat}, {max_lon} {max_lat}, {min_lon} {max_lat}, {min_lon} {min_lat}))"


def get_places_query(release: str, bbox: tuple[float, float, float, float]) -> str:
    """Generate DuckDB query for fetching places data."""
    bbox_wkt = bbox_to_wkt(bbox)
    return f"""
        SELECT
            id as gers_id,
            names.primary as name,
            categories.primary as category,
            ST_X(geometry) as lon,
            ST_Y(geometry) as lat,
            bbox.xmin as bbox_xmin,
            bbox.ymin as bbox_ymin,
            bbox.xmax as bbox_xmax,
            bbox.ymax as bbox_ymax,
            addresses[1].freeform as address,
            addresses[1].locality as city,
            addresses[1].region as region,
            addresses[1].country as country,
            confidence
        FROM read_parquet(
            's3://overturemaps-us-west-2/release/{release}/theme=places/type=place/*',
            hive_partitioning = true
        )
        WHERE ST_Within(geometry, ST_GeomFromText('{bbox_wkt}'))
          AND confidence >= 0.5
    """


def get_addresses_query(release: str, bbox: tuple[float, float, float, float]) -> str:
    """Generate DuckDB query for fetching addresses data (US only)."""
    bbox_wkt = bbox_to_wkt(bbox)
    return f"""
        SELECT
            id as gers_id,
            number,
            street,
            unit,
            postcode,
            address_levels[2].value as city,
            address_levels[1].value as state,
            'US' as country,
            ST_X(geometry) as lon,
            ST_Y(geometry) as lat,
            bbox.xmin as bbox_xmin,
            bbox.ymin as bbox_ymin,
            bbox.xmax as bbox_xmax,
            bbox.ymax as bbox_ymax
        FROM read_parquet(
            's3://overturemaps-us-west-2/release/{release}/theme=addresses/type=address/*',
            hive_partitioning = true
        )
        WHERE ST_Within(geometry, ST_GeomFromText('{bbox_wkt}'))
          AND country = 'US'
    """


def get_streets_query(release: str, bbox: tuple[float, float, float, float]) -> str:
    """Generate DuckDB query for fetching street data from transportation segments."""
    bbox_wkt = bbox_to_wkt(bbox)
    return f"""
        SELECT
            names.primary as street_name,
            class as road_class,
            id as segment_id,
            ST_X(ST_Centroid(geometry)) as lon,
            ST_Y(ST_Centroid(geometry)) as lat
        FROM read_parquet(
            's3://overturemaps-us-west-2/release/{release}/theme=transportation/type=segment/*',
            hive_partitioning = true
        )
        WHERE ST_Within(ST_Centroid(geometry), ST_GeomFromText('{bbox_wkt}'))
          AND names.primary IS NOT NULL
          AND subtype = 'road'
    """


def aggregate_streets(records: list) -> list:
    """
    Aggregate transportation segments to unique streets.

    Returns list of dicts with:
    - street_name: The street name
    - street_lower: Normalized for search
    - road_class: Best road class (for ranking)
    - segment_count: Number of segments
    - lon, lat: Centroid of all segments
    - segment_id: ID of first segment (for reference)
    """
    from collections import defaultdict

    # Group by normalized street name
    street_groups = defaultdict(list)
    for record in records:
        name = record.get('street_name') or ''
        if name:
            key = normalize_text(name)
            street_groups[key].append(record)

    # Road class priority (higher = better for ranking)
    class_priority = {
        'motorway': 10,
        'trunk': 9,
        'primary': 8,
        'secondary': 7,
        'tertiary': 6,
        'residential': 5,
        'service': 4,
        'unclassified': 3,
        'living_street': 2,
        'pedestrian': 1,
    }

    aggregated = []
    for street_lower, segments in street_groups.items():
        # Get best road class
        best_class = max(
            segments,
            key=lambda s: class_priority.get(s.get('road_class', ''), 0)
        ).get('road_class', 'unclassified')

        # Calculate centroid of all segments
        lons = [s['lon'] for s in segments if s.get('lon')]
        lats = [s['lat'] for s in segments if s.get('lat')]

        if lons and lats:
            aggregated.append({
                'street_name': segments[0].get('street_name', ''),
                'street_lower': street_lower,
                'road_class': best_class,
                'segment_count': len(segments),
                'lon': sum(lons) / len(lons),
                'lat': sum(lats) / len(lats),
                'segment_id': segments[0].get('segment_id', ''),
            })

    return aggregated


def fetch_data(
    query: str,
    output_path: Path,
    description: str,
    dry_run: bool = False,
) -> dict:
    """Execute DuckDB query and save to parquet, return metrics."""
    if dry_run:
        print(f"\n[DRY RUN] Would execute {description} query:")
        print(f"  Output: {output_path}")
        print(f"  Query preview:\n{query[:500]}...")
        return {"record_count": 0, "size_bytes": 0, "dry_run": True}

    output_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"\nFetching {description}...")
    print(f"  Output: {output_path}")

    con = duckdb.connect()

    # Configure for S3 access
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("INSTALL spatial; LOAD spatial;")
    con.execute("SET s3_region = 'us-west-2';")
    con.execute("SET memory_limit = '4GB';")

    # Execute query and save to parquet
    copy_query = f"COPY ({query}) TO '{output_path}' (FORMAT PARQUET, COMPRESSION ZSTD)"
    con.execute(copy_query)

    # Get row count
    count_result = con.execute(f"SELECT COUNT(*) FROM read_parquet('{output_path}')").fetchone()
    record_count = count_result[0] if count_result else 0

    con.close()

    size_bytes = output_path.stat().st_size if output_path.exists() else 0
    print(f"  Records: {record_count:,}")
    print(f"  Size: {size_bytes / 1024 / 1024:.2f} MB")

    return {"record_count": record_count, "size_bytes": size_bytes}


def build_places_region_schema(db: sqlite3.Connection):
    """Create schema for places region shard (full data + FTS)."""
    db.executescript("""
        PRAGMA journal_mode=DELETE;
        PRAGMA synchronous=NORMAL;
        PRAGMA cache_size=-64000;

        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS places (
            rowid INTEGER PRIMARY KEY,
            gers_id TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            category TEXT,
            lon REAL NOT NULL,
            lat REAL NOT NULL,
            bbox_xmin REAL,
            bbox_ymin REAL,
            bbox_xmax REAL,
            bbox_ymax REAL,
            address TEXT,
            city TEXT,
            region TEXT,
            country TEXT,
            confidence REAL,
            search_text TEXT NOT NULL
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS places_fts USING fts5(
            search_text,
            content=places,
            content_rowid=rowid,
            tokenize='porter unicode61 remove_diacritics 1',
            prefix='2 3'
        );

        CREATE TRIGGER IF NOT EXISTS places_ai AFTER INSERT ON places BEGIN
            INSERT INTO places_fts(rowid, search_text)
            VALUES (new.rowid, new.search_text);
        END;
    """)


def build_places_index_schema(db: sqlite3.Connection):
    """Create schema for places index shard (lightweight, name + coords only)."""
    db.executescript("""
        PRAGMA journal_mode=DELETE;
        PRAGMA synchronous=NORMAL;
        PRAGMA cache_size=-64000;

        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS places_index (
            rowid INTEGER PRIMARY KEY,
            gers_id TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            category TEXT,
            lon REAL NOT NULL,
            lat REAL NOT NULL,
            search_text TEXT NOT NULL
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS places_fts USING fts5(
            search_text,
            content=places_index,
            content_rowid=rowid,
            tokenize='porter unicode61 remove_diacritics 1',
            prefix='2 3'
        );

        CREATE TRIGGER IF NOT EXISTS places_ai AFTER INSERT ON places_index BEGIN
            INSERT INTO places_fts(rowid, search_text)
            VALUES (new.rowid, new.search_text);
        END;
    """)


def build_places_detail_schema(db: sqlite3.Connection):
    """Create schema for places detail shard (full data, no FTS)."""
    db.executescript("""
        PRAGMA journal_mode=DELETE;
        PRAGMA synchronous=NORMAL;
        PRAGMA cache_size=-64000;

        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS places_detail (
            rowid INTEGER PRIMARY KEY,
            gers_id TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            category TEXT,
            lon REAL NOT NULL,
            lat REAL NOT NULL,
            bbox_xmin REAL,
            bbox_ymin REAL,
            bbox_xmax REAL,
            bbox_ymax REAL,
            address TEXT,
            city TEXT,
            region TEXT,
            country TEXT,
            confidence REAL
        );

        CREATE INDEX IF NOT EXISTS idx_gers_id ON places_detail(gers_id);
    """)


def build_addresses_region_schema(db: sqlite3.Connection):
    """Create schema for addresses region shard (full data + FTS)."""
    db.executescript("""
        PRAGMA journal_mode=DELETE;
        PRAGMA synchronous=NORMAL;
        PRAGMA cache_size=-64000;

        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS addresses (
            rowid INTEGER PRIMARY KEY,
            gers_id TEXT NOT NULL UNIQUE,
            number TEXT,
            street TEXT,
            unit TEXT,
            postcode TEXT,
            city TEXT,
            state TEXT,
            country TEXT,
            lon REAL NOT NULL,
            lat REAL NOT NULL,
            bbox_xmin REAL,
            bbox_ymin REAL,
            bbox_xmax REAL,
            bbox_ymax REAL,
            search_text TEXT NOT NULL
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS addresses_fts USING fts5(
            search_text,
            content=addresses,
            content_rowid=rowid,
            tokenize='porter unicode61 remove_diacritics 1',
            prefix='2 3'
        );

        CREATE TRIGGER IF NOT EXISTS addresses_ai AFTER INSERT ON addresses BEGIN
            INSERT INTO addresses_fts(rowid, search_text)
            VALUES (new.rowid, new.search_text);
        END;
    """)


def build_addresses_index_schema(db: sqlite3.Connection):
    """Create schema for addresses index shard (lightweight)."""
    db.executescript("""
        PRAGMA journal_mode=DELETE;
        PRAGMA synchronous=NORMAL;
        PRAGMA cache_size=-64000;

        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS addresses_index (
            rowid INTEGER PRIMARY KEY,
            gers_id TEXT NOT NULL UNIQUE,
            street TEXT,
            city TEXT,
            state TEXT,
            postcode TEXT,
            lon REAL NOT NULL,
            lat REAL NOT NULL,
            search_text TEXT NOT NULL
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS addresses_fts USING fts5(
            search_text,
            content=addresses_index,
            content_rowid=rowid,
            tokenize='porter unicode61 remove_diacritics 1',
            prefix='2 3'
        );

        CREATE TRIGGER IF NOT EXISTS addresses_ai AFTER INSERT ON addresses_index BEGIN
            INSERT INTO addresses_fts(rowid, search_text)
            VALUES (new.rowid, new.search_text);
        END;
    """)


def build_addresses_detail_schema(db: sqlite3.Connection):
    """Create schema for addresses detail shard (full data, no FTS)."""
    db.executescript("""
        PRAGMA journal_mode=DELETE;
        PRAGMA synchronous=NORMAL;
        PRAGMA cache_size=-64000;

        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS addresses_detail (
            rowid INTEGER PRIMARY KEY,
            gers_id TEXT NOT NULL UNIQUE,
            number TEXT,
            street TEXT,
            unit TEXT,
            postcode TEXT,
            city TEXT,
            state TEXT,
            country TEXT,
            lon REAL NOT NULL,
            lat REAL NOT NULL,
            bbox_xmin REAL,
            bbox_ymin REAL,
            bbox_xmax REAL,
            bbox_ymax REAL
        );

        CREATE INDEX IF NOT EXISTS idx_gers_id ON addresses_detail(gers_id);
    """)


def build_places_prefix_schema(db: sqlite3.Connection):
    """Create schema for places prefix-only shard (B-tree index, no FTS)."""
    db.executescript("""
        PRAGMA journal_mode=DELETE;
        PRAGMA synchronous=NORMAL;
        PRAGMA cache_size=-64000;

        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS places (
            rowid INTEGER PRIMARY KEY,
            gers_id TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            name_lower TEXT NOT NULL,
            category TEXT,
            lon REAL NOT NULL,
            lat REAL NOT NULL,
            city TEXT,
            region TEXT,
            country TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_name_lower ON places(name_lower);
    """)


def build_places_minimal_schema(db: sqlite3.Connection):
    """Create ultra-minimal schema for places (just what's needed for search + hydration)."""
    db.executescript("""
        PRAGMA journal_mode=DELETE;
        PRAGMA synchronous=NORMAL;
        PRAGMA cache_size=-64000;

        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        -- Ultra-minimal: just gers_id for hydration, name_lower for search, lat/lon for ranking
        -- No UNIQUE constraint, no extra indexes beyond the search index
        CREATE TABLE IF NOT EXISTS places (
            rowid INTEGER PRIMARY KEY,
            gers_id TEXT NOT NULL,
            name_lower TEXT NOT NULL,
            lon REAL NOT NULL,
            lat REAL NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_name_lower ON places(name_lower);
    """)


def build_addresses_prefix_schema(db: sqlite3.Connection):
    """Create schema for addresses prefix-only shard (B-tree index, no FTS)."""
    db.executescript("""
        PRAGMA journal_mode=DELETE;
        PRAGMA synchronous=NORMAL;
        PRAGMA cache_size=-64000;

        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS addresses (
            rowid INTEGER PRIMARY KEY,
            gers_id TEXT NOT NULL UNIQUE,
            street TEXT,
            street_lower TEXT,
            city TEXT,
            state TEXT,
            postcode TEXT,
            lon REAL NOT NULL,
            lat REAL NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_street_lower ON addresses(street_lower);
    """)


def build_addresses_minimal_schema(db: sqlite3.Connection):
    """Create ultra-minimal schema for addresses (just what's needed for search + hydration)."""
    db.executescript("""
        PRAGMA journal_mode=DELETE;
        PRAGMA synchronous=NORMAL;
        PRAGMA cache_size=-64000;

        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        -- Ultra-minimal: gers_id for hydration, street_lower for search, lat/lon for ranking
        CREATE TABLE IF NOT EXISTS addresses (
            rowid INTEGER PRIMARY KEY,
            gers_id TEXT NOT NULL,
            street_lower TEXT NOT NULL,
            lon REAL NOT NULL,
            lat REAL NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_street_lower ON addresses(street_lower);
    """)


def normalize_text(text: str) -> str:
    """Normalize text for prefix search (lowercase, basic cleanup)."""
    if not text:
        return ""
    # Simple normalization: lowercase and strip
    # Could add unicodedata.normalize + remove diacritics if needed
    return text.lower().strip()


# ============================================================================
# Radix Trie Implementation for Compact Prefix Search
# ============================================================================

class RadixTrieNode:
    """Node in a radix trie (compact prefix trie)."""
    __slots__ = ['edge_label', 'children', 'values']

    def __init__(self, edge_label: str = ""):
        self.edge_label = edge_label  # Edge label from parent to this node
        self.children: dict[str, 'RadixTrieNode'] = {}  # First char -> child node
        self.values: list = []  # Values stored at this node (for complete matches)


class RadixTrie:
    """
    Compact prefix trie (radix tree) for efficient autocomplete.

    Instead of one node per character (standard trie), this merges
    chains of single-child nodes into edges with multi-char labels.

    Example: ["starbucks", "starbucks coffee", "subway"]
    Standard trie: s->t->a->r->b->u->c->k->s (9 nodes for "starbucks")
    Radix trie: "starbu"->cks (2 nodes, edge labels instead of char-per-node)
    """

    def __init__(self):
        self.root = RadixTrieNode()
        self.size = 0

    def insert(self, key: str, value: any):
        """Insert a key-value pair into the trie."""
        if not key:
            return

        node = self.root
        i = 0

        while i < len(key):
            first_char = key[i]

            if first_char not in node.children:
                # No matching edge, create new node with remaining key as edge
                new_node = RadixTrieNode(key[i:])
                new_node.values.append(value)
                node.children[first_char] = new_node
                self.size += 1
                return

            child = node.children[first_char]
            edge = child.edge_label

            # Find common prefix between remaining key and edge label
            j = 0
            while j < len(edge) and i + j < len(key) and edge[j] == key[i + j]:
                j += 1

            if j == len(edge):
                # Entire edge matches, continue down
                i += j
                if i == len(key):
                    # Key exhausted at this node
                    child.values.append(value)
                    self.size += 1
                    return
                node = child
            else:
                # Partial match - need to split the edge
                # Create intermediate node at split point
                split_node = RadixTrieNode(edge[:j])

                # Update child's edge to be remainder after split
                child.edge_label = edge[j:]
                split_node.children[edge[j]] = child

                # Insert split node into parent
                node.children[first_char] = split_node

                if i + j == len(key):
                    # Key exhausted at split point
                    split_node.values.append(value)
                else:
                    # Create new leaf for remaining key
                    new_leaf = RadixTrieNode(key[i + j:])
                    new_leaf.values.append(value)
                    split_node.children[key[i + j]] = new_leaf

                self.size += 1
                return

        # Key exhausted, add value to current node
        node.values.append(value)
        self.size += 1

    def prefix_search(self, prefix: str, limit: int = 20) -> list:
        """Find all values with keys starting with prefix."""
        if not prefix:
            return []

        node = self.root
        i = 0

        # Navigate to node matching prefix
        while i < len(prefix):
            first_char = prefix[i]

            if first_char not in node.children:
                return []  # No match

            child = node.children[first_char]
            edge = child.edge_label

            # Check if edge matches remaining prefix
            remaining_prefix = prefix[i:]
            if edge.startswith(remaining_prefix):
                # Prefix exhausted within edge - collect all descendants
                return self._collect_values(child, limit)
            elif remaining_prefix.startswith(edge):
                # Edge exhausted, continue with rest of prefix
                i += len(edge)
                node = child
            else:
                return []  # No match

        # Collect all values from this node and descendants
        return self._collect_values(node, limit)

    def _collect_values(self, node: RadixTrieNode, limit: int) -> list:
        """Collect all values from node and its descendants."""
        results = []
        stack = [node]

        while stack and len(results) < limit:
            current = stack.pop()
            results.extend(current.values[:limit - len(results)])
            # Add children in reverse order so we process them in alphabetical order
            for char in sorted(current.children.keys(), reverse=True):
                if len(results) >= limit:
                    break
                stack.append(current.children[char])

        return results[:limit]

    def serialize(self) -> bytes:
        """
        Serialize trie to compact binary format.

        Format per node:
        - edge_label_len (2 bytes, uint16)
        - edge_label (utf-8 bytes)
        - num_values (2 bytes, uint16)
        - values: each value is gers_id (GERS ID is 16 bytes as UUID) + lon (4 bytes float) + lat (4 bytes float)
        - num_children (1 byte, uint8)
        - for each child: first_char (1 byte) + child node (recursive)
        """
        buffer = bytearray()
        self._serialize_node(self.root, buffer)
        return bytes(buffer)

    def _serialize_node(self, node: RadixTrieNode, buffer: bytearray):
        """Recursively serialize a node."""
        # Edge label
        edge_bytes = node.edge_label.encode('utf-8')
        buffer.extend(struct.pack('<H', len(edge_bytes)))
        buffer.extend(edge_bytes)

        # Values (each is: gers_id string length + gers_id + lon + lat)
        buffer.extend(struct.pack('<H', len(node.values)))
        for value in node.values:
            gers_id = value.get('gers_id', '')
            lon = value.get('lon', 0.0)
            lat = value.get('lat', 0.0)
            gers_bytes = gers_id.encode('utf-8')
            buffer.extend(struct.pack('<B', len(gers_bytes)))  # gers_id length
            buffer.extend(gers_bytes)
            buffer.extend(struct.pack('<ff', lon, lat))

        # Children
        buffer.extend(struct.pack('<B', len(node.children)))
        for char in sorted(node.children.keys()):
            buffer.extend(char.encode('utf-8')[:1])  # First char as single byte
            self._serialize_node(node.children[char], buffer)

    def to_json(self) -> dict:
        """Serialize trie to JSON-compatible dict (for debugging/comparison)."""
        return self._node_to_dict(self.root)

    def _node_to_dict(self, node: RadixTrieNode) -> dict:
        """Convert node to dict."""
        result = {}
        if node.edge_label:
            result['e'] = node.edge_label
        if node.values:
            result['v'] = node.values
        if node.children:
            result['c'] = {
                char: self._node_to_dict(child)
                for char, child in sorted(node.children.items())
            }
        return result


def build_radix_trie(records: list, key_field: str) -> RadixTrie:
    """Build a radix trie from records."""
    trie = RadixTrie()

    for record in records:
        key = normalize_text(record.get(key_field) or "")
        if key:
            # Store minimal data needed for search results
            value = {
                'gers_id': record.get('gers_id', ''),
                'lon': record.get('lon', 0.0),
                'lat': record.get('lat', 0.0),
            }
            trie.insert(key, value)

    return trie


def save_trie_shard(trie: RadixTrie, output_path: Path, use_binary: bool = True) -> int:
    """Save trie to file, return size in bytes."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if use_binary:
        data = trie.serialize()
        with open(output_path, 'wb') as f:
            f.write(data)
    else:
        # JSON fallback for debugging
        data = json.dumps(trie.to_json(), separators=(',', ':'))
        with open(output_path, 'w') as f:
            f.write(data)

    return output_path.stat().st_size


def build_places_search_text(row: dict) -> str:
    """Build search text for a place record."""
    parts = [
        row.get("name") or "",
        row.get("category") or "",
        row.get("city") or "",
        row.get("region") or "",
        row.get("country") or "",
        row.get("address") or "",
    ]
    return " ".join(p.lower() for p in parts if p).strip()


def build_addresses_search_text(row: dict) -> str:
    """Build search text for an address record."""
    parts = [
        row.get("number") or "",
        row.get("street") or "",
        row.get("city") or "",
        row.get("state") or "",
        row.get("postcode") or "",
        row.get("country") or "",
    ]
    return " ".join(p.lower() for p in parts if p).strip()


def build_places_shards(
    parquet_path: Path,
    output_dir: Path,
    shard_id: str,
    dry_run: bool = False,
    no_fts: bool = False,
) -> dict:
    """Build places shards (region, tiered, and optionally prefix-only strategies)."""
    empty_result = {
        "region": {"size_bytes": 0, "record_count": 0},
        "tiered_index": {"size_bytes": 0, "record_count": 0},
        "tiered_detail": {"size_bytes": 0, "record_count": 0},
    }
    if no_fts:
        empty_result["prefix_only"] = {"size_bytes": 0, "record_count": 0}
        empty_result["minimal"] = {"size_bytes": 0, "record_count": 0}
        empty_result["trie"] = {"size_bytes": 0, "record_count": 0}

    if dry_run or not parquet_path.exists():
        return empty_result

    print(f"\nBuilding places shards for {shard_id}...")

    con = duckdb.connect()
    rows = con.execute(f"SELECT * FROM read_parquet('{parquet_path}')").fetchall()
    columns = [desc[0] for desc in con.description]
    con.close()

    if not rows:
        print("  No places data to process")
        return empty_result

    # Convert to list of dicts
    records = [dict(zip(columns, row)) for row in rows]

    results = {}

    # Strategy 1: Region-level shard (all data + FTS)
    region_dir = output_dir / "region"
    region_dir.mkdir(parents=True, exist_ok=True)
    region_path = region_dir / f"places-{shard_id}.db"

    if region_path.exists():
        region_path.unlink()

    db = sqlite3.connect(region_path)
    build_places_region_schema(db)

    for record in records:
        search_text = build_places_search_text(record)
        db.execute("""
            INSERT INTO places (
                gers_id, name, category, lon, lat,
                bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax,
                address, city, region, country, confidence, search_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            record["gers_id"], record["name"], record.get("category"),
            record["lon"], record["lat"],
            record.get("bbox_xmin"), record.get("bbox_ymin"),
            record.get("bbox_xmax"), record.get("bbox_ymax"),
            record.get("address"), record.get("city"),
            record.get("region"), record.get("country"),
            record.get("confidence"), search_text,
        ))

    db.execute("INSERT OR REPLACE INTO metadata VALUES ('record_count', ?)", (str(len(records)),))
    db.execute("INSERT INTO places_fts(places_fts) VALUES('optimize')")
    db.commit()
    db.execute("VACUUM")
    db.close()

    results["region"] = {
        "size_bytes": region_path.stat().st_size,
        "record_count": len(records),
    }
    print(f"  Region shard: {len(records):,} records, {results['region']['size_bytes'] / 1024 / 1024:.2f} MB")

    # Strategy 2: Tiered - Index shard (lightweight)
    tiered_dir = output_dir / "tiered"
    tiered_dir.mkdir(parents=True, exist_ok=True)

    index_path = tiered_dir / f"places-index-{shard_id}.db"
    if index_path.exists():
        index_path.unlink()

    db = sqlite3.connect(index_path)
    build_places_index_schema(db)

    for record in records:
        search_text = build_places_search_text(record)
        db.execute("""
            INSERT INTO places_index (
                gers_id, name, category, lon, lat, search_text
            ) VALUES (?, ?, ?, ?, ?, ?)
        """, (
            record["gers_id"], record["name"], record.get("category"),
            record["lon"], record["lat"], search_text,
        ))

    db.execute("INSERT OR REPLACE INTO metadata VALUES ('record_count', ?)", (str(len(records)),))
    db.execute("INSERT INTO places_fts(places_fts) VALUES('optimize')")
    db.commit()
    db.execute("VACUUM")
    db.close()

    results["tiered_index"] = {
        "size_bytes": index_path.stat().st_size,
        "record_count": len(records),
    }
    print(f"  Tiered index: {len(records):,} records, {results['tiered_index']['size_bytes'] / 1024 / 1024:.2f} MB")

    # Strategy 2: Tiered - Detail shard (full data, no FTS)
    detail_path = tiered_dir / f"places-detail-{shard_id}.db"
    if detail_path.exists():
        detail_path.unlink()

    db = sqlite3.connect(detail_path)
    build_places_detail_schema(db)

    for record in records:
        db.execute("""
            INSERT INTO places_detail (
                gers_id, name, category, lon, lat,
                bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax,
                address, city, region, country, confidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            record["gers_id"], record["name"], record.get("category"),
            record["lon"], record["lat"],
            record.get("bbox_xmin"), record.get("bbox_ymin"),
            record.get("bbox_xmax"), record.get("bbox_ymax"),
            record.get("address"), record.get("city"),
            record.get("region"), record.get("country"),
            record.get("confidence"),
        ))

    db.execute("INSERT OR REPLACE INTO metadata VALUES ('record_count', ?)", (str(len(records)),))
    db.commit()
    db.execute("VACUUM")
    db.close()

    results["tiered_detail"] = {
        "size_bytes": detail_path.stat().st_size,
        "record_count": len(records),
    }
    print(f"  Tiered detail: {len(records):,} records, {results['tiered_detail']['size_bytes'] / 1024 / 1024:.2f} MB")

    # Strategy 3: Prefix-only (B-tree index, no FTS) - only if --no-fts flag
    if no_fts:
        prefix_dir = output_dir / "prefix"
        prefix_dir.mkdir(parents=True, exist_ok=True)

        prefix_path = prefix_dir / f"places-{shard_id}.db"
        if prefix_path.exists():
            prefix_path.unlink()

        db = sqlite3.connect(prefix_path)
        build_places_prefix_schema(db)

        for record in records:
            name_lower = normalize_text(record.get("name") or "")
            db.execute("""
                INSERT INTO places (
                    gers_id, name, name_lower, category, lon, lat,
                    city, region, country
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                record["gers_id"], record["name"], name_lower,
                record.get("category"),
                record["lon"], record["lat"],
                record.get("city"), record.get("region"), record.get("country"),
            ))

        db.execute("INSERT OR REPLACE INTO metadata VALUES ('record_count', ?)", (str(len(records)),))
        db.commit()
        db.execute("VACUUM")
        db.close()

        results["prefix_only"] = {
            "size_bytes": prefix_path.stat().st_size,
            "record_count": len(records),
        }
        print(f"  Prefix-only: {len(records):,} records, {results['prefix_only']['size_bytes'] / 1024 / 1024:.2f} MB")

        # Strategy 4: Minimal (ultra-compact SQLite, just gers_id + name_lower + coords)
        minimal_dir = output_dir / "minimal"
        minimal_dir.mkdir(parents=True, exist_ok=True)

        minimal_path = minimal_dir / f"places-{shard_id}.db"
        if minimal_path.exists():
            minimal_path.unlink()

        db = sqlite3.connect(minimal_path)
        build_places_minimal_schema(db)

        for record in records:
            name_lower = normalize_text(record.get("name") or "")
            db.execute("""
                INSERT INTO places (gers_id, name_lower, lon, lat)
                VALUES (?, ?, ?, ?)
            """, (
                record["gers_id"], name_lower,
                record["lon"], record["lat"],
            ))

        db.execute("INSERT OR REPLACE INTO metadata VALUES ('record_count', ?)", (str(len(records)),))
        db.commit()
        db.execute("VACUUM")
        db.close()

        results["minimal"] = {
            "size_bytes": minimal_path.stat().st_size,
            "record_count": len(records),
        }
        print(f"  Minimal: {len(records):,} records, {results['minimal']['size_bytes'] / 1024 / 1024:.2f} MB")

        # Strategy 5: Radix Trie (serialized binary trie)
        trie_dir = output_dir / "trie"
        trie_dir.mkdir(parents=True, exist_ok=True)

        trie = build_radix_trie(records, "name")
        trie_path = trie_dir / f"places-{shard_id}.trie"
        trie_size = save_trie_shard(trie, trie_path, use_binary=True)

        results["trie"] = {
            "size_bytes": trie_size,
            "record_count": len(records),
        }
        print(f"  Radix trie: {len(records):,} records, {trie_size / 1024 / 1024:.2f} MB")

    return results


def build_addresses_shards(
    parquet_path: Path,
    output_dir: Path,
    shard_id: str,
    dry_run: bool = False,
    no_fts: bool = False,
) -> dict:
    """Build addresses shards (region, tiered, and optionally prefix-only strategies)."""
    empty_result = {
        "region": {"size_bytes": 0, "record_count": 0},
        "tiered_index": {"size_bytes": 0, "record_count": 0},
        "tiered_detail": {"size_bytes": 0, "record_count": 0},
    }
    if no_fts:
        empty_result["prefix_only"] = {"size_bytes": 0, "record_count": 0}
        empty_result["minimal"] = {"size_bytes": 0, "record_count": 0}
        empty_result["trie"] = {"size_bytes": 0, "record_count": 0}

    if dry_run or not parquet_path.exists():
        return empty_result

    print(f"\nBuilding addresses shards for {shard_id}...")

    con = duckdb.connect()
    rows = con.execute(f"SELECT * FROM read_parquet('{parquet_path}')").fetchall()
    columns = [desc[0] for desc in con.description]
    con.close()

    if not rows:
        print("  No addresses data to process")
        return empty_result

    # Convert to list of dicts
    records = [dict(zip(columns, row)) for row in rows]

    results = {}

    # Strategy 1: Region-level shard (all data + FTS)
    region_dir = output_dir / "region"
    region_dir.mkdir(parents=True, exist_ok=True)
    region_path = region_dir / f"addresses-{shard_id}.db"

    if region_path.exists():
        region_path.unlink()

    db = sqlite3.connect(region_path)
    build_addresses_region_schema(db)

    for record in records:
        search_text = build_addresses_search_text(record)
        db.execute("""
            INSERT INTO addresses (
                gers_id, number, street, unit, postcode,
                city, state, country, lon, lat,
                bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax, search_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            record["gers_id"], record.get("number"), record.get("street"),
            record.get("unit"), record.get("postcode"),
            record.get("city"), record.get("state"), record.get("country"),
            record["lon"], record["lat"],
            record.get("bbox_xmin"), record.get("bbox_ymin"),
            record.get("bbox_xmax"), record.get("bbox_ymax"),
            search_text,
        ))

    db.execute("INSERT OR REPLACE INTO metadata VALUES ('record_count', ?)", (str(len(records)),))
    db.execute("INSERT INTO addresses_fts(addresses_fts) VALUES('optimize')")
    db.commit()
    db.execute("VACUUM")
    db.close()

    results["region"] = {
        "size_bytes": region_path.stat().st_size,
        "record_count": len(records),
    }
    print(f"  Region shard: {len(records):,} records, {results['region']['size_bytes'] / 1024 / 1024:.2f} MB")

    # Strategy 2: Tiered - Index shard (lightweight)
    tiered_dir = output_dir / "tiered"
    tiered_dir.mkdir(parents=True, exist_ok=True)

    index_path = tiered_dir / f"addresses-index-{shard_id}.db"
    if index_path.exists():
        index_path.unlink()

    db = sqlite3.connect(index_path)
    build_addresses_index_schema(db)

    for record in records:
        search_text = build_addresses_search_text(record)
        db.execute("""
            INSERT INTO addresses_index (
                gers_id, street, city, state, postcode, lon, lat, search_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            record["gers_id"], record.get("street"), record.get("city"),
            record.get("state"), record.get("postcode"),
            record["lon"], record["lat"], search_text,
        ))

    db.execute("INSERT OR REPLACE INTO metadata VALUES ('record_count', ?)", (str(len(records)),))
    db.execute("INSERT INTO addresses_fts(addresses_fts) VALUES('optimize')")
    db.commit()
    db.execute("VACUUM")
    db.close()

    results["tiered_index"] = {
        "size_bytes": index_path.stat().st_size,
        "record_count": len(records),
    }
    print(f"  Tiered index: {len(records):,} records, {results['tiered_index']['size_bytes'] / 1024 / 1024:.2f} MB")

    # Strategy 2: Tiered - Detail shard (full data, no FTS)
    detail_path = tiered_dir / f"addresses-detail-{shard_id}.db"
    if detail_path.exists():
        detail_path.unlink()

    db = sqlite3.connect(detail_path)
    build_addresses_detail_schema(db)

    for record in records:
        db.execute("""
            INSERT INTO addresses_detail (
                gers_id, number, street, unit, postcode,
                city, state, country, lon, lat,
                bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            record["gers_id"], record.get("number"), record.get("street"),
            record.get("unit"), record.get("postcode"),
            record.get("city"), record.get("state"), record.get("country"),
            record["lon"], record["lat"],
            record.get("bbox_xmin"), record.get("bbox_ymin"),
            record.get("bbox_xmax"), record.get("bbox_ymax"),
        ))

    db.execute("INSERT OR REPLACE INTO metadata VALUES ('record_count', ?)", (str(len(records)),))
    db.commit()
    db.execute("VACUUM")
    db.close()

    results["tiered_detail"] = {
        "size_bytes": detail_path.stat().st_size,
        "record_count": len(records),
    }
    print(f"  Tiered detail: {len(records):,} records, {results['tiered_detail']['size_bytes'] / 1024 / 1024:.2f} MB")

    # Strategy 3: Prefix-only (B-tree index, no FTS) - only if --no-fts flag
    if no_fts:
        prefix_dir = output_dir / "prefix"
        prefix_dir.mkdir(parents=True, exist_ok=True)

        prefix_path = prefix_dir / f"addresses-{shard_id}.db"
        if prefix_path.exists():
            prefix_path.unlink()

        db = sqlite3.connect(prefix_path)
        build_addresses_prefix_schema(db)

        for record in records:
            street_lower = normalize_text(record.get("street") or "")
            db.execute("""
                INSERT INTO addresses (
                    gers_id, street, street_lower, city, state, postcode, lon, lat
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                record["gers_id"], record.get("street"), street_lower,
                record.get("city"), record.get("state"), record.get("postcode"),
                record["lon"], record["lat"],
            ))

        db.execute("INSERT OR REPLACE INTO metadata VALUES ('record_count', ?)", (str(len(records)),))
        db.commit()
        db.execute("VACUUM")
        db.close()

        results["prefix_only"] = {
            "size_bytes": prefix_path.stat().st_size,
            "record_count": len(records),
        }
        print(f"  Prefix-only: {len(records):,} records, {results['prefix_only']['size_bytes'] / 1024 / 1024:.2f} MB")

        # Strategy 4: Minimal (ultra-compact SQLite, just gers_id + street_lower + coords)
        minimal_dir = output_dir / "minimal"
        minimal_dir.mkdir(parents=True, exist_ok=True)

        minimal_path = minimal_dir / f"addresses-{shard_id}.db"
        if minimal_path.exists():
            minimal_path.unlink()

        db = sqlite3.connect(minimal_path)
        build_addresses_minimal_schema(db)

        for record in records:
            street_lower = normalize_text(record.get("street") or "")
            db.execute("""
                INSERT INTO addresses (gers_id, street_lower, lon, lat)
                VALUES (?, ?, ?, ?)
            """, (
                record["gers_id"], street_lower,
                record["lon"], record["lat"],
            ))

        db.execute("INSERT OR REPLACE INTO metadata VALUES ('record_count', ?)", (str(len(records)),))
        db.commit()
        db.execute("VACUUM")
        db.close()

        results["minimal"] = {
            "size_bytes": minimal_path.stat().st_size,
            "record_count": len(records),
        }
        print(f"  Minimal: {len(records):,} records, {results['minimal']['size_bytes'] / 1024 / 1024:.2f} MB")

        # Strategy 5: Radix Trie (serialized binary trie)
        trie_dir = output_dir / "trie"
        trie_dir.mkdir(parents=True, exist_ok=True)

        trie = build_radix_trie(records, "street")
        trie_path = trie_dir / f"addresses-{shard_id}.trie"
        trie_size = save_trie_shard(trie, trie_path, use_binary=True)

        results["trie"] = {
            "size_bytes": trie_size,
            "record_count": len(records),
        }
        print(f"  Radix trie: {len(records):,} records, {trie_size / 1024 / 1024:.2f} MB")

    return results


def build_streets_schema(db: sqlite3.Connection):
    """Create schema for streets index (from transportation segments)."""
    db.executescript("""
        PRAGMA journal_mode=DELETE;
        PRAGMA synchronous=NORMAL;
        PRAGMA cache_size=-64000;

        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        -- Street index: street_name for display, street_lower for search
        CREATE TABLE IF NOT EXISTS streets (
            rowid INTEGER PRIMARY KEY,
            segment_id TEXT NOT NULL,
            street_name TEXT NOT NULL,
            street_lower TEXT NOT NULL,
            road_class TEXT,
            segment_count INTEGER,
            lon REAL NOT NULL,
            lat REAL NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_street_lower ON streets(street_lower);
    """)


def build_streets_shards(
    parquet_path: Path,
    output_dir: Path,
    shard_id: str,
    dry_run: bool = False,
) -> dict:
    """Build street index shards from transportation segments."""
    empty_result = {
        "sqlite": {"size_bytes": 0, "record_count": 0, "unique_streets": 0},
        "trie": {"size_bytes": 0, "record_count": 0, "unique_streets": 0},
    }

    if dry_run or not parquet_path.exists():
        return empty_result

    print(f"\nBuilding street index shards for {shard_id}...")

    con = duckdb.connect()
    rows = con.execute(f"SELECT * FROM read_parquet('{parquet_path}')").fetchall()
    columns = [desc[0] for desc in con.description]
    con.close()

    if not rows:
        print("  No street data to process")
        return empty_result

    # Convert to list of dicts
    records = [dict(zip(columns, row)) for row in rows]
    print(f"  Raw segments: {len(records):,}")

    # Aggregate to unique streets
    streets = aggregate_streets(records)
    print(f"  Unique streets: {len(streets):,}")

    if not streets:
        return empty_result

    results = {}

    # Strategy 1: SQLite with B-tree index
    sqlite_dir = output_dir / "streets"
    sqlite_dir.mkdir(parents=True, exist_ok=True)

    sqlite_path = sqlite_dir / f"streets-{shard_id}.db"
    if sqlite_path.exists():
        sqlite_path.unlink()

    db = sqlite3.connect(sqlite_path)
    build_streets_schema(db)

    for street in streets:
        db.execute("""
            INSERT INTO streets (
                segment_id, street_name, street_lower, road_class,
                segment_count, lon, lat
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            street['segment_id'], street['street_name'], street['street_lower'],
            street['road_class'], street['segment_count'],
            street['lon'], street['lat'],
        ))

    db.execute("INSERT OR REPLACE INTO metadata VALUES ('record_count', ?)", (str(len(streets)),))
    db.commit()
    db.execute("VACUUM")
    db.close()

    results["sqlite"] = {
        "size_bytes": sqlite_path.stat().st_size,
        "record_count": len(records),
        "unique_streets": len(streets),
    }
    print(f"  SQLite: {len(streets):,} streets, {results['sqlite']['size_bytes'] / 1024:.2f} KB")

    # Strategy 2: Radix Trie
    trie_dir = output_dir / "streets"

    # Build trie using street_name as key
    trie = RadixTrie()
    for street in streets:
        key = street['street_lower']
        if key:
            value = {
                'gers_id': street['segment_id'],
                'name': street['street_name'],
                'lon': street['lon'],
                'lat': street['lat'],
            }
            trie.insert(key, value)

    trie_path = trie_dir / f"streets-{shard_id}.trie"
    trie_size = save_trie_shard(trie, trie_path, use_binary=True)

    results["trie"] = {
        "size_bytes": trie_size,
        "record_count": len(records),
        "unique_streets": len(streets),
    }
    print(f"  Radix trie: {len(streets):,} streets, {trie_size / 1024:.2f} KB")

    return results


def format_size(size_bytes: int) -> str:
    """Format bytes as human-readable string."""
    if size_bytes >= 1024 * 1024 * 1024:
        return f"{size_bytes / 1024 / 1024 / 1024:.2f} GB"
    elif size_bytes >= 1024 * 1024:
        return f"{size_bytes / 1024 / 1024:.2f} MB"
    elif size_bytes >= 1024:
        return f"{size_bytes / 1024:.2f} KB"
    else:
        return f"{size_bytes} bytes"


def print_metrics_summary(metrics: dict):
    """Print a summary of the metrics."""
    print("\n" + "=" * 60)
    print("EXPERIMENT RESULTS SUMMARY")
    print("=" * 60)

    if "places" in metrics:
        print("\nPLACES:")
        p = metrics["places"]
        print(f"  Raw data: {p.get('raw', {}).get('record_count', 0):,} records, "
              f"{format_size(p.get('raw', {}).get('size_bytes', 0))}")

        if "shards" in p:
            s = p["shards"]
            print(f"\n  Region strategy:")
            print(f"    Size: {format_size(s.get('region', {}).get('size_bytes', 0))}")
            print(f"    Records: {s.get('region', {}).get('record_count', 0):,}")

            print(f"\n  Tiered strategy:")
            print(f"    Index: {format_size(s.get('tiered_index', {}).get('size_bytes', 0))}")
            print(f"    Detail: {format_size(s.get('tiered_detail', {}).get('size_bytes', 0))}")
            print(f"    Combined: {format_size(s.get('tiered_index', {}).get('size_bytes', 0) + s.get('tiered_detail', {}).get('size_bytes', 0))}")

            # Prefix-only strategy (if available)
            if "prefix_only" in s:
                print(f"\n  Prefix-only strategy (no FTS):")
                print(f"    Size: {format_size(s.get('prefix_only', {}).get('size_bytes', 0))}")
                print(f"    Records: {s.get('prefix_only', {}).get('record_count', 0):,}")

            # Minimal strategy (if available)
            if "minimal" in s:
                print(f"\n  Minimal strategy (ultra-compact):")
                print(f"    Size: {format_size(s.get('minimal', {}).get('size_bytes', 0))}")
                print(f"    Records: {s.get('minimal', {}).get('record_count', 0):,}")

            # Radix trie strategy (if available)
            if "trie" in s:
                print(f"\n  Radix trie strategy:")
                print(f"    Size: {format_size(s.get('trie', {}).get('size_bytes', 0))}")
                print(f"    Records: {s.get('trie', {}).get('record_count', 0):,}")

            # Calculate savings
            region_size = s.get('region', {}).get('size_bytes', 0)
            index_size = s.get('tiered_index', {}).get('size_bytes', 0)
            prefix_size = s.get('prefix_only', {}).get('size_bytes', 0)
            minimal_size = s.get('minimal', {}).get('size_bytes', 0)
            trie_size = s.get('trie', {}).get('size_bytes', 0)

            if region_size > 0:
                reduction = (1 - index_size / region_size) * 100
                print(f"\n  Tiered Index vs Region: {reduction:.1f}% smaller")

            if prefix_size > 0 and region_size > 0:
                reduction = (1 - prefix_size / region_size) * 100
                print(f"  Prefix-only vs Region: {reduction:.1f}% smaller")

            if minimal_size > 0 and region_size > 0:
                reduction = (1 - minimal_size / region_size) * 100
                print(f"  Minimal vs Region: {reduction:.1f}% smaller")

            if trie_size > 0 and region_size > 0:
                reduction = (1 - trie_size / region_size) * 100
                print(f"  Radix trie vs Region: {reduction:.1f}% smaller")

    if "addresses" in metrics:
        print("\nADDRESSES:")
        a = metrics["addresses"]
        print(f"  Raw data: {a.get('raw', {}).get('record_count', 0):,} records, "
              f"{format_size(a.get('raw', {}).get('size_bytes', 0))}")

        if "shards" in a:
            s = a["shards"]
            print(f"\n  Region strategy:")
            print(f"    Size: {format_size(s.get('region', {}).get('size_bytes', 0))}")
            print(f"    Records: {s.get('region', {}).get('record_count', 0):,}")

            print(f"\n  Tiered strategy:")
            print(f"    Index: {format_size(s.get('tiered_index', {}).get('size_bytes', 0))}")
            print(f"    Detail: {format_size(s.get('tiered_detail', {}).get('size_bytes', 0))}")
            print(f"    Combined: {format_size(s.get('tiered_index', {}).get('size_bytes', 0) + s.get('tiered_detail', {}).get('size_bytes', 0))}")

            # Prefix-only strategy (if available)
            if "prefix_only" in s:
                print(f"\n  Prefix-only strategy (no FTS):")
                print(f"    Size: {format_size(s.get('prefix_only', {}).get('size_bytes', 0))}")
                print(f"    Records: {s.get('prefix_only', {}).get('record_count', 0):,}")

            # Minimal strategy (if available)
            if "minimal" in s:
                print(f"\n  Minimal strategy (ultra-compact):")
                print(f"    Size: {format_size(s.get('minimal', {}).get('size_bytes', 0))}")
                print(f"    Records: {s.get('minimal', {}).get('record_count', 0):,}")

            # Radix trie strategy (if available)
            if "trie" in s:
                print(f"\n  Radix trie strategy:")
                print(f"    Size: {format_size(s.get('trie', {}).get('size_bytes', 0))}")
                print(f"    Records: {s.get('trie', {}).get('record_count', 0):,}")

            # Calculate savings
            region_size = s.get('region', {}).get('size_bytes', 0)
            index_size = s.get('tiered_index', {}).get('size_bytes', 0)
            prefix_size = s.get('prefix_only', {}).get('size_bytes', 0)
            minimal_size = s.get('minimal', {}).get('size_bytes', 0)
            trie_size = s.get('trie', {}).get('size_bytes', 0)

            if region_size > 0:
                reduction = (1 - index_size / region_size) * 100
                print(f"\n  Tiered Index vs Region: {reduction:.1f}% smaller")

            if prefix_size > 0 and region_size > 0:
                reduction = (1 - prefix_size / region_size) * 100
                print(f"  Prefix-only vs Region: {reduction:.1f}% smaller")

            if minimal_size > 0 and region_size > 0:
                reduction = (1 - minimal_size / region_size) * 100
                print(f"  Minimal vs Region: {reduction:.1f}% smaller")

            if trie_size > 0 and region_size > 0:
                reduction = (1 - trie_size / region_size) * 100
                print(f"  Radix trie vs Region: {reduction:.1f}% smaller")

    if "streets" in metrics:
        print("\nSTREETS (from transportation):")
        s = metrics["streets"]
        print(f"  Raw segments: {s.get('raw', {}).get('record_count', 0):,} records, "
              f"{format_size(s.get('raw', {}).get('size_bytes', 0))}")

        if "shards" in s:
            shards = s["shards"]
            unique_streets = shards.get('sqlite', {}).get('unique_streets', 0)
            print(f"  Unique streets: {unique_streets:,}")

            print(f"\n  SQLite strategy:")
            print(f"    Size: {format_size(shards.get('sqlite', {}).get('size_bytes', 0))}")

            print(f"\n  Radix trie strategy:")
            print(f"    Size: {format_size(shards.get('trie', {}).get('size_bytes', 0))}")

            # Compare to addresses
            if "addresses" in metrics and metrics["addresses"].get("shards"):
                addr_trie = metrics["addresses"]["shards"].get("trie", {}).get("size_bytes", 0)
                street_trie = shards.get("trie", {}).get("size_bytes", 0)
                if addr_trie > 0 and street_trie > 0:
                    reduction = (1 - street_trie / addr_trie) * 100
                    print(f"\n  Street trie vs Address trie: {reduction:.1f}% smaller!")

    # Scaling estimates
    print("\n" + "-" * 60)
    print("SCALING ESTIMATES (extrapolating to full US):")
    print("-" * 60)

    if "places" in metrics and metrics["places"].get("shards"):
        p = metrics["places"]["shards"]
        record_count = p.get("region", {}).get("record_count", 0)
        region_size = p.get("region", {}).get("size_bytes", 0)
        index_size = p.get("tiered_index", {}).get("size_bytes", 0)
        prefix_size = p.get("prefix_only", {}).get("size_bytes", 0)
        minimal_size = p.get("minimal", {}).get("size_bytes", 0)
        trie_size = p.get("trie", {}).get("size_bytes", 0)

        if record_count > 0:
            # Estimate ~15M places in US
            scale_factor = 15_000_000 / record_count
            print(f"\n  US Places (~15M records, scale factor: {scale_factor:.0f}x):")
            print(f"    Region per-state: ~{format_size(int(region_size * scale_factor / 50))}")
            print(f"    Tiered Index per-state: ~{format_size(int(index_size * scale_factor / 50))}")
            if prefix_size > 0:
                print(f"    Prefix-only per-state: ~{format_size(int(prefix_size * scale_factor / 50))}")
            if minimal_size > 0:
                print(f"    Minimal per-state: ~{format_size(int(minimal_size * scale_factor / 50))}")
            if trie_size > 0:
                print(f"    Radix trie per-state: ~{format_size(int(trie_size * scale_factor / 50))}")
            print(f"    Total region: ~{format_size(int(region_size * scale_factor))}")
            if minimal_size > 0:
                print(f"    Total minimal: ~{format_size(int(minimal_size * scale_factor))}")
            if trie_size > 0:
                print(f"    Total radix trie: ~{format_size(int(trie_size * scale_factor))}")

    if "addresses" in metrics and metrics["addresses"].get("shards"):
        a = metrics["addresses"]["shards"]
        record_count = a.get("region", {}).get("record_count", 0)
        region_size = a.get("region", {}).get("size_bytes", 0)
        index_size = a.get("tiered_index", {}).get("size_bytes", 0)
        prefix_size = a.get("prefix_only", {}).get("size_bytes", 0)
        minimal_size = a.get("minimal", {}).get("size_bytes", 0)
        trie_size = a.get("trie", {}).get("size_bytes", 0)

        if record_count > 0:
            # Estimate ~300M addresses in US
            scale_factor = 300_000_000 / record_count
            print(f"\n  US Addresses (~300M records, scale factor: {scale_factor:.0f}x):")
            print(f"    Region per-state: ~{format_size(int(region_size * scale_factor / 50))}")
            print(f"    Tiered Index per-state: ~{format_size(int(index_size * scale_factor / 50))}")
            if prefix_size > 0:
                print(f"    Prefix-only per-state: ~{format_size(int(prefix_size * scale_factor / 50))}")
            if minimal_size > 0:
                print(f"    Minimal per-state: ~{format_size(int(minimal_size * scale_factor / 50))}")
            if trie_size > 0:
                print(f"    Radix trie per-state: ~{format_size(int(trie_size * scale_factor / 50))}")
            print(f"    Total region: ~{format_size(int(region_size * scale_factor))}")
            if minimal_size > 0:
                print(f"    Total minimal: ~{format_size(int(minimal_size * scale_factor))}")
            if trie_size > 0:
                print(f"    Total radix trie: ~{format_size(int(trie_size * scale_factor))}")

    if "streets" in metrics and metrics["streets"].get("shards"):
        s = metrics["streets"]["shards"]
        segment_count = s.get("sqlite", {}).get("record_count", 0)
        unique_streets = s.get("sqlite", {}).get("unique_streets", 0)
        trie_size = s.get("trie", {}).get("size_bytes", 0)

        if unique_streets > 0:
            # Estimate ~3M unique streets in US
            scale_factor = 3_000_000 / unique_streets
            print(f"\n  US Streets (~3M unique streets, scale factor: {scale_factor:.0f}x):")
            if trie_size > 0:
                print(f"    Trie per-state: ~{format_size(int(trie_size * scale_factor / 50))}")
                print(f"    Total trie: ~{format_size(int(trie_size * scale_factor))}")

    print("\n" + "=" * 60)


def main():
    parser = argparse.ArgumentParser(
        description="Experiment with places/addresses sharding strategies",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Tiny test (dry run)
  python scripts/experiment_places_addresses.py --bbox "-122.42,37.78,-122.41,37.79" --dry-run

  # San Francisco
  python scripts/experiment_places_addresses.py --bbox "-122.52,37.70,-122.35,37.82"

  # California (on larger machine)
  python scripts/experiment_places_addresses.py --region US-CA --output-dir /data/experiment
        """,
    )

    parser.add_argument(
        "--bbox",
        help='Bounding box "min_lon,min_lat,max_lon,max_lat"',
    )
    parser.add_argument(
        "--region",
        help="US region code (e.g., US-CA, US-NY)",
    )
    parser.add_argument(
        "--release",
        help="Overture release version (default: latest)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Output directory (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--no-places",
        action="store_true",
        help="Skip places data",
    )
    parser.add_argument(
        "--no-addresses",
        action="store_true",
        help="Skip addresses data",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show queries without executing",
    )
    parser.add_argument(
        "--no-fts",
        action="store_true",
        help="Build prefix-only shards (no FTS5) for size comparison",
    )
    parser.add_argument(
        "--streets",
        action="store_true",
        help="Build street index from transportation segments (for address autocomplete)",
    )

    args = parser.parse_args()

    # Validate inputs
    if not args.bbox and not args.region:
        parser.error("Either --bbox or --region is required")

    if args.bbox and args.region:
        parser.error("Specify either --bbox or --region, not both")

    # Parse bbox or get from region
    if args.bbox:
        try:
            parts = [float(x.strip()) for x in args.bbox.split(",")]
            if len(parts) != 4:
                raise ValueError("Expected 4 values")
            bbox = tuple(parts)
            shard_id = "bbox"
        except ValueError as e:
            parser.error(f"Invalid bbox format: {e}")
    else:
        if args.region not in US_STATE_BBOXES:
            parser.error(f"Unknown region: {args.region}. Available: {', '.join(sorted(US_STATE_BBOXES.keys()))}")
        bbox = US_STATE_BBOXES[args.region]
        shard_id = args.region

    # Get release version
    release = args.release
    if not release:
        print("Fetching latest Overture release...")
        try:
            release = get_latest_release()
        except Exception as e:
            print(f"Warning: Could not fetch latest release ({e}), using fallback")
            release = "2025-12-17.0"
    print(f"Using Overture release: {release}")

    # Setup output directory
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"\nExperiment Configuration:")
    print(f"  Bbox: {bbox}")
    print(f"  Shard ID: {shard_id}")
    print(f"  Output: {output_dir}")
    print(f"  Places: {'yes' if not args.no_places else 'no'}")
    print(f"  Addresses: {'yes' if not args.no_addresses else 'no'}")
    print(f"  Streets (from transportation): {'yes' if args.streets else 'no'}")
    print(f"  Prefix-only (no FTS): {'yes' if args.no_fts else 'no'}")
    print(f"  Dry run: {'yes' if args.dry_run else 'no'}")

    metrics = {
        "bbox": bbox,
        "shard_id": shard_id,
        "release": release,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    # Fetch and process places
    if not args.no_places:
        places_query = get_places_query(release, bbox)
        places_parquet = output_dir / "places-raw.parquet"
        places_raw_metrics = fetch_data(
            places_query, places_parquet, "places", args.dry_run
        )
        metrics["places"] = {"raw": places_raw_metrics}

        # Build shards
        places_shard_metrics = build_places_shards(
            places_parquet, output_dir, shard_id, args.dry_run, args.no_fts
        )
        metrics["places"]["shards"] = places_shard_metrics

    # Fetch and process addresses
    if not args.no_addresses:
        addresses_query = get_addresses_query(release, bbox)
        addresses_parquet = output_dir / "addresses-raw.parquet"
        addresses_raw_metrics = fetch_data(
            addresses_query, addresses_parquet, "addresses", args.dry_run
        )
        metrics["addresses"] = {"raw": addresses_raw_metrics}

        # Build shards
        addresses_shard_metrics = build_addresses_shards(
            addresses_parquet, output_dir, shard_id, args.dry_run, args.no_fts
        )
        metrics["addresses"]["shards"] = addresses_shard_metrics

    # Fetch and process streets (from transportation segments)
    if args.streets:
        streets_query = get_streets_query(release, bbox)
        streets_parquet = output_dir / "streets-raw.parquet"
        streets_raw_metrics = fetch_data(
            streets_query, streets_parquet, "streets (transportation)", args.dry_run
        )
        metrics["streets"] = {"raw": streets_raw_metrics}

        # Build shards
        streets_shard_metrics = build_streets_shards(
            streets_parquet, output_dir, shard_id, args.dry_run
        )
        metrics["streets"]["shards"] = streets_shard_metrics

    # Save metrics
    metrics_path = output_dir / "metrics.json"
    with open(metrics_path, "w") as f:
        json.dump(metrics, f, indent=2)
    print(f"\nMetrics saved to: {metrics_path}")

    # Print summary
    if not args.dry_run:
        print_metrics_summary(metrics)


if __name__ == "__main__":
    main()
