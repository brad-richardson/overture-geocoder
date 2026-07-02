#!/usr/bin/env python3
"""
Build country-partitioned SQLite shards for R2 storage.

This script orchestrates the full shard build pipeline:
1. Downloads divisions data from Overture (if needed)
2. Builds per-country SQLite shards with FTS5
3. Builds HEAD shard (countries, regions, large cities)
4. Generates STAC catalog for shard discovery

Usage:
    python scripts/build_shards.py [--version VERSION] [--head-threshold 100000]
    python scripts/build_shards.py --countries US,CA,GB  # Build specific countries only
    python scripts/build_shards.py --head-only           # Build HEAD shard only
    python scripts/build_shards.py --reverse             # Build reverse geocoding shards

Output:
    shards/{version}/
        shards/HEAD.db
        shards/US.db
        shards/CA.db
        ...
        items/HEAD.json
        items/US.json
        ...
        collection.json
    shards/catalog.json

For reverse geocoding (--reverse):
    shards/{version}/
        reverse/HEAD.db
        reverse/US.db
        ...
        reverse-items/HEAD.json
        ...
        reverse-collection.json
"""

import argparse
import json
import hashlib
import math
import re
import shutil
import sqlite3
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import duckdb

# Validation patterns
COUNTRY_CODE_PATTERN = re.compile(r'^[A-Z]{2}$')
REGION_CODE_PATTERN = re.compile(r'^[A-Z]{2}-[A-Z0-9]{1,3}$')

# Shard size threshold for region splitting (50MB to stay under 128MB CF worker limit)
SHARD_SIZE_THRESHOLD_BYTES = 50 * 1024 * 1024  # 50MB

# Countries with at least this many records are routed straight to region
# shards without building the country shard first. Deliberately conservative
# (even at a low ~150 bytes/record with FTS, 400k records exceeds the 50MB
# size threshold) so borderline countries still get the authoritative
# build-then-measure treatment and shard layout is unchanged.
REGION_SPLIT_RECORD_THRESHOLD = 400_000

# FTS5 prefix index lengths (higher values improve autocomplete at ~5-10% per extra level)
FTS5_PREFIX_LENGTHS = '2 3 4'

# FTS5 tokenizer. NO porter stemmer: porter stems "france" -> "franc", which
# prefix-matches "francisco"; nobody stems toponyms (docs/ranking-research.md, P0).
FTS5_TOKENIZER = 'unicode61 remove_diacritics 2'

# Bidirectional abbreviation pairs for search alias generation
# (Placeholder's lib/analysis.js set, see docs/ranking-research.md P5)
ABBREVIATION_PAIRS = [
    ("saint", "st"), ("sainte", "ste"),
    ("fort", "ft"), ("mount", "mt"),
    ("north", "n"), ("south", "s"),
    ("east", "e"), ("west", "w"),
    ("port", "pt"), ("point", "pt"),
]

# One-directional aliases: the right side is emitted when the left side
# appears as a token, but not vice versa ("&" never survives unicode61
# tokenization, so the reverse mapping would index nothing).
ONE_WAY_ALIASES = {
    "&": {"and"},
}

# German-convention foldings emitted as alias variants ("münchen" -> "muenchen").
# remove_diacritics 2 already folds "ü" -> "u", so the plain-ASCII form matches
# without help; these cover the ue/oe/ae/ss spellings users actually type.
UMLAUT_FOLDINGS = [("ü", "ue"), ("ö", "oe"), ("ä", "ae"), ("ß", "ss")]

# Apostrophe characters stripped to form alias variants ("john's" -> "johns")
APOSTROPHE_CHARS = "'’"

# Designation-strip patterns (Placeholder lib/analysis.js): when the primary
# name matches, the bare name is emitted as an alias ("Cook County" -> "cook").
DESIGNATION_PATTERNS = [
    re.compile(r"^city of (.+)$", re.IGNORECASE),
    re.compile(r"^county of (.+)$", re.IGNORECASE),
    re.compile(r"^(.+) county$", re.IGNORECASE),
]

# Fallback region suffix for records with null region codes
FALLBACK_REGION_SUFFIX = "XX"

# ---------------------------------------------------------------------------
# Ranking constants for the precomputed static `importance` column in [0, 1].
# See docs/ranking-research.md (P1 wikidata importance, P2 type prior +
# dampened population). importance = min(1.0, type_prior + 0.5*wiki +
# pop_component + capital_bonus); the worker ranks bm25 - k*importance.
# ---------------------------------------------------------------------------

# Static prior by Overture subtype (Photon searchPrio shape: city above
# county/state - do NOT copy Nominatim's county-above-city address ranks)
TYPE_PRIOR = {
    "country": 0.30,
    "region": 0.18,
    "county": 0.10,
    "localadmin": 0.08,
    "macrohood": 0.05,
    "neighborhood": 0.04,
}

# Localities are further split by Overture's `class` enum
LOCALITY_CLASS_PRIOR = {
    "megacity": 0.30,
    "city": 0.22,
    "town": 0.14,
    "village": 0.06,
    "hamlet": 0.04,
}
LOCALITY_DEFAULT_PRIOR = 0.10  # locality with null/unknown class

# Subtypes not listed above (dependency, microhood, ...)
DEFAULT_TYPE_PRIOR = 0.05

# wiki_component = WIKI_IMPORTANCE_WEIGHT * wiki_importance (0 when unmatched)
WIKI_IMPORTANCE_WEIGHT = 0.5

# pop_component = min(CAP, COEFF * log10(1 + population)), halved for the
# coarse subtypes below (population should only differentiate within-type)
POP_COMPONENT_CAP = 0.2
POP_COMPONENT_COEFF = 0.03
POP_HALF_WEIGHT_SUBTYPES = {"country", "dependency", "region", "county", "localadmin"}

# Capital bonus from Overture's capital_of_divisions
CAPITAL_COUNTRY_BONUS = 0.08
CAPITAL_REGION_BONUS = 0.03

# Nominatim's published QID-keyed wikimedia importance file (P1). Despite the
# .csv name it is tab-separated with a header:
#   language, type, title, importance, wikidata_id
# importance is in [0, 1] (log-of-inbound-links, normalized).
WIKIMEDIA_IMPORTANCE_URL = "https://nominatim.org/data/wikimedia-importance.csv.gz"

# nominatim.org returns 403 for default curl/wget user agents
DOWNLOAD_USER_AGENT = "overture-geocoder/1.0 (shard build pipeline)"

# Small localities (at or below the download's population bar) survive the
# enrichment prune when their wiki importance reaches this; they land in
# their country/region shard. Measured on the 2026-07-02.0 build: ~60k
# localities clear 0.5.
WIKI_LOCALITY_KEEP_THRESHOLD = 0.5

# HEAD additionally includes famous-but-small places (wiki_importance >=
# this). HEAD is loaded on EVERY search and lives in the worker's 64 MB
# shard cache, so this bar is deliberately much higher than the keep
# threshold: 0.65 admits ~2.4k world-famous places (Gettysburg is 0.80),
# where 0.5 admitted ~60k and tripled HEAD's size.
HEAD_WIKI_IMPORTANCE_THRESHOLD = 0.65


def build_search_alias(name: str, search_name: str) -> str | None:
    """
    Build the search_alias column from a division's primary name and its
    search_name tokens (docs/ranking-research.md P4/P5).

    Emits (space-separated, deduplicated):
    - Pairwise adjacent concatenations of the primary name: "new york" -> "newyork"
    - Full concatenations of 2-4 word name prefixes: "new york city" -> "newyorkcity"
    - Abbreviation variants of search_name tokens: "saint" <-> "st", "&" -> "and"
    - Umlaut/eszett foldings: "münchen" -> "muenchen", "gießen" -> "giessen"
    - Apostrophe-stripped variants: "john's" -> "johns"
    - Designation-stripped names: "Cook County" -> "cook", "City of X" -> "x"

    Returns None when there is nothing to add.
    """
    name = (name or "").strip()
    search_name = (search_name or "").strip()
    name_words = name.lower().split()
    tokens = search_name.lower().split()
    if not tokens and not name_words:
        return None

    extras: list[str] = []
    seen = set(tokens)

    def add(token: str):
        if token and token not in seen:
            extras.append(token)
            seen.add(token)

    # --- Designation strips on the primary name ---
    for pattern in DESIGNATION_PATTERNS:
        m = pattern.match(name)
        if m:
            for word in m.group(1).lower().split():
                if word and word not in extras:
                    extras.append(word)
                    seen.add(word)
            break

    # --- Concatenated forms from the primary name ---
    primary_words = [w for w in name_words[:4] if w.isalpha()]
    if not primary_words:
        # Fall back to leading search_name tokens (legacy behavior)
        primary_words = [w for w in tokens[:4] if w.isalpha()]

    # Pairwise adjacent concatenations
    for i in range(len(primary_words) - 1):
        concat = primary_words[i] + primary_words[i + 1]
        if 4 <= len(concat) <= 30:
            add(concat)

    # Full concatenations for 2-4 word prefixes
    if len(primary_words) >= 2:
        max_len = min(4, len(primary_words))
        for n in range(2, max_len + 1):
            full = "".join(primary_words[:n])
            if 4 <= len(full) <= 30:
                add(full)

    # --- Abbreviation variants (bidirectional + one-way) ---
    abbrev_from: dict[str, set[str]] = {}
    for long, short in ABBREVIATION_PAIRS:
        abbrev_from.setdefault(long, set()).add(short)
        abbrev_from.setdefault(short, set()).add(long)
    for source, variants in ONE_WAY_ALIASES.items():
        abbrev_from.setdefault(source, set()).update(variants)

    for word in tokens:
        if word in abbrev_from:
            for variant in sorted(abbrev_from[word]):
                add(variant)

    # --- Character-level variants of every name token ---
    for word in tokens:
        # Umlaut/eszett foldings (German-convention spellings)
        folded = word
        for char, repl in UMLAUT_FOLDINGS:
            folded = folded.replace(char, repl)
        if folded != word:
            add(folded)

        # Apostrophe-stripped variants
        stripped = word
        for char in APOSTROPHE_CHARS:
            stripped = stripped.replace(char, "")
        if stripped != word and len(stripped) >= 2:
            add(stripped)

    if not extras:
        return None
    return " ".join(extras)


def compute_importance(
    subtype: str,
    class_: str | None = None,
    population: int | None = None,
    wiki_importance: float | None = None,
    is_country_capital: bool = False,
    is_region_capital: bool = False,
) -> float:
    """
    Precompute the static prominence score in [0, 1] for a division.

    importance = min(1.0, type_prior + wiki_component + pop_component +
    capital_bonus). Constants above; rationale in docs/ranking-research.md
    (P1/P2). The query side ranks with `bm25(...) - k * importance`.
    """
    if subtype == "locality":
        prior = LOCALITY_CLASS_PRIOR.get(class_, LOCALITY_DEFAULT_PRIOR)
    else:
        prior = TYPE_PRIOR.get(subtype, DEFAULT_TYPE_PRIOR)

    wiki_component = 0.0
    if wiki_importance is not None:
        wiki_component = WIKI_IMPORTANCE_WEIGHT * max(0.0, min(1.0, wiki_importance))

    pop_component = 0.0
    if population is not None and population > 0:
        pop_component = min(
            POP_COMPONENT_CAP, POP_COMPONENT_COEFF * math.log10(1 + population)
        )
        if subtype in POP_HALF_WEIGHT_SUBTYPES:
            pop_component *= 0.5

    capital_bonus = 0.0
    if is_country_capital:
        capital_bonus = CAPITAL_COUNTRY_BONUS
    elif is_region_capital:
        capital_bonus = CAPITAL_REGION_BONUS

    return min(1.0, prior + wiki_component + pop_component + capital_bonus)


def validate_country_code(code: str) -> str:
    """Validate and return a country code, or raise ValueError."""
    if not COUNTRY_CODE_PATTERN.match(code):
        raise ValueError(f"Invalid country code: {code!r} (must be 2 uppercase letters)")
    return code

def validate_population_threshold(threshold: int) -> int:
    """Validate population threshold is a reasonable positive integer."""
    if not isinstance(threshold, int) or threshold < 0 or threshold > 10_000_000_000:
        raise ValueError(f"Invalid population threshold: {threshold}")
    return threshold


def validate_region_code(code: str) -> str:
    """Validate and return a region code, or raise ValueError."""
    # Allow fallback region codes like "CN-XX"
    if code.endswith(f"-{FALLBACK_REGION_SUFFIX}"):
        country = code[:2]
        if COUNTRY_CODE_PATTERN.match(country):
            return code
    if not REGION_CODE_PATTERN.match(code):
        raise ValueError(f"Invalid region code: {code!r} (must be like 'US-MA' or 'CN-GD')")
    return code

# Default paths
EXPORTS_DIR = Path("exports")
SHARDS_DIR = Path("shards")
DIVISIONS_PARQUET = EXPORTS_DIR / "divisions-global.parquet"
DIVISIONS_REVERSE_PARQUET = EXPORTS_DIR / "divisions-reverse.parquet"

# Local cache for the Nominatim wikimedia importance file (a few hundred MB;
# the download is skipped when this file already exists)
WIKIMEDIA_IMPORTANCE_FILE = EXPORTS_DIR / "wikimedia-importance.csv.gz"

# HEAD shard includes countries, regions, and localities with pop >= threshold
DEFAULT_HEAD_THRESHOLD = 100_000

# Local scratch directory for country-partitioned parquet (one global pass
# instead of one full parquet scan per country)
PARTITIONS_DIR = EXPORTS_DIR / "partitions"


def get_version(suffix: str = "0") -> str:
    """Get version string (date-based with suffix)."""
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"{date}.{suffix}"


def version_sort_key(version: str) -> tuple[str, int]:
    """Sort key for '{YYYY-MM-DD}.{N}' versions: date part lexicographic,
    .N suffix numeric (plain string order ranks .9 above .10)."""
    date, _, n = version.rpartition(".")
    if date and n.isdigit():
        return (date, int(n))
    return (version, 0)


def spill_safe_connect(memory_limit: str = "10GB") -> "duckdb.DuckDBPyConnection":
    """In-memory DuckDB connection hardened for the CI runner.

    In-memory sessions disable disk spill by default, so a big join or
    aggregation OOMs instead of going out-of-core (the DuckDB 1.5 failure
    mode the download SQL scripts guard against). Cap memory, provide a
    spill directory, and bound parallel pipeline buffers — mirroring the
    settings in download_divisions_global.sql.
    """
    con = duckdb.connect()
    con.execute(f"SET memory_limit = '{memory_limit}';")
    spill_dir = Path(tempfile.gettempdir()) / "duckdb_spill.tmp"
    con.execute(f"SET temp_directory = '{spill_dir}';")
    con.execute("SET threads = 2;")
    return con


def get_countries(parquet_path: Path) -> list[str]:
    """Get list of unique country codes from parquet file."""
    parquet_str = str(parquet_path.resolve())
    con = duckdb.connect()
    result = con.execute(f"""
        SELECT DISTINCT country
        FROM read_parquet('{parquet_str}')
        WHERE country IS NOT NULL
        ORDER BY country
    """).fetchall()
    con.close()
    return [r[0] for r in result]


def partition_by_country(
    parquet_path: Path,
    partition_dir: Path,
    countries: list[str] | None = None,
) -> dict[str, int]:
    """
    Partition the global parquet by country in a single pass.

    Writes hive-partitioned parquet (country=XX/data_0.parquet) to
    partition_dir so per-country shard builds read only their own
    partition instead of re-scanning the global file per country.

    Returns dict of country code -> record count (used to route obviously
    oversized countries straight to region builds).
    """
    parquet_str = str(parquet_path.resolve())
    if partition_dir.exists():
        shutil.rmtree(partition_dir)
    partition_dir.mkdir(parents=True, exist_ok=True)

    where = "country IS NOT NULL"
    if countries:
        codes = ", ".join(f"'{validate_country_code(c)}'" for c in countries)
        where += f" AND country IN ({codes})"

    con = spill_safe_connect()
    # Partitioned COPY keeps a write buffer per country; row order within
    # partitions is irrelevant (shard builds re-query), and preserving
    # insertion order is the main memory amplifier in COPY.
    con.execute("SET preserve_insertion_order = false;")

    # WRITE_PARTITION_COLUMNS keeps the country column in the data files so
    # downstream shard builders see the exact same schema as the global file.
    con.execute(f"""
        COPY (
            SELECT * FROM read_parquet('{parquet_str}')
            WHERE {where}
        ) TO '{str(partition_dir.resolve())}'
        (FORMAT PARQUET, PARTITION_BY (country),
         WRITE_PARTITION_COLUMNS true, OVERWRITE_OR_IGNORE true);
    """)

    # Count from the written partitions (near metadata-only) instead of a
    # second full decompress pass over the global file.
    try:
        counts = dict(con.execute(f"""
            SELECT country, COUNT(*)
            FROM read_parquet('{str(partition_dir.resolve())}/country=*/*.parquet')
            GROUP BY country
        """).fetchall())
    except duckdb.Error as exc:
        if "No files found" not in str(exc):
            raise
        counts = {}  # selection matched no rows; no partitions written
    con.close()
    return counts


def country_partition_glob(partition_dir: Path, country_code: str) -> Path:
    """Glob path for a single country's partitioned parquet files."""
    country_code = validate_country_code(country_code)
    return partition_dir / f"country={country_code}" / "*.parquet"


def get_regions_for_country(parquet_path: Path, country_code: str) -> list[tuple[str, int]]:
    """
    Get list of regions and their record counts for a country.

    Returns list of (region_code, record_count) tuples, including a fallback
    region for records with null region codes.
    """
    country_code = validate_country_code(country_code)
    parquet_str = str(parquet_path.resolve())
    fallback_region = f"{country_code}-{FALLBACK_REGION_SUFFIX}"

    con = duckdb.connect()
    result = con.execute(f"""
        SELECT
            COALESCE(region, '{fallback_region}') as region,
            COUNT(*) as cnt
        FROM read_parquet('{parquet_str}')
        WHERE country = '{country_code}'
        GROUP BY region
        ORDER BY cnt DESC
    """).fetchall()
    con.close()

    return [(r[0], r[1]) for r in result]


def ensure_wiki_importance_file(
    dest: Path = WIKIMEDIA_IMPORTANCE_FILE,
    url: str = WIKIMEDIA_IMPORTANCE_URL,
) -> Path:
    """
    Download Nominatim's wikimedia importance file unless already cached.

    Fails loudly on any download error (use --no-wiki-importance to build
    without wikidata importance).
    """
    if dest.exists() and dest.stat().st_size > 0:
        print(f"Using cached wikimedia importance file: {dest}")
        return dest

    print(f"Downloading wikimedia importance file from {url} ...")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest.with_suffix(dest.suffix + ".tmp")
    request = urllib.request.Request(url, headers={"User-Agent": DOWNLOAD_USER_AGENT})
    try:
        # Socket timeout per read: a hung nominatim.org connection would
        # otherwise stall the monthly build until the 6h Actions limit.
        with urllib.request.urlopen(request, timeout=60) as response, open(tmp_path, "wb") as f:
            shutil.copyfileobj(response, f, length=1024 * 1024)
        tmp_path.rename(dest)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    print(f"Downloaded {dest} ({dest.stat().st_size / 1024 / 1024:.1f} MB)")
    return dest


def enrich_parquet_with_wiki_importance(
    parquet_path: Path,
    output_path: Path,
    importance_file: Path | None,
) -> Path:
    """
    LEFT JOIN the wikimedia importance file onto the divisions parquet by
    wikidata QID, producing a `wiki_importance DOUBLE` column (NULL when
    unmatched). With importance_file=None the column is added as all-NULL so
    downstream shard builds see a uniform schema.

    Also prunes small localities that aren't famous: the download SQL
    over-fetches every locality with a wikidata QID (population alone would
    exclude famous-but-small places like Gettysburg), and this is the single
    chokepoint where importance is known. Kept rows: non-localities,
    localities over the download's population bar, and localities at or
    above WIKI_LOCALITY_KEEP_THRESHOLD.
    """
    parquet_str = str(parquet_path.resolve())
    output_str = str(output_path.resolve())
    output_path.parent.mkdir(parents=True, exist_ok=True)

    prune = (
        "subtype != 'locality' "
        "OR population > 10000 "
        f"OR wiki_importance >= {WIKI_LOCALITY_KEEP_THRESHOLD}"
    )

    con = spill_safe_connect()
    con.execute("SET preserve_insertion_order = false;")
    if importance_file is None:
        con.execute(f"""
            COPY (
                SELECT * FROM (
                    SELECT *, CAST(NULL AS DOUBLE) AS wiki_importance
                    FROM read_parquet('{parquet_str}')
                ) WHERE {prune}
            ) TO '{output_str}' (FORMAT PARQUET, COMPRESSION ZSTD);
        """)
    else:
        importance_str = str(importance_file.resolve())
        # Tab-separated despite the .csv name; quote='' because titles can
        # contain double quotes. One row per (language, title): aggregate to
        # the best importance per QID.
        con.execute(f"""
            COPY (
                SELECT * FROM (
                    SELECT d.*, w.wiki_importance
                    FROM read_parquet('{parquet_str}') d
                    LEFT JOIN (
                        SELECT wikidata_id, MAX(importance) AS wiki_importance
                        FROM read_csv(
                            '{importance_str}',
                            delim='\t', header=true, quote='',
                            columns={{
                                'language': 'VARCHAR',
                                'type': 'VARCHAR',
                                'title': 'VARCHAR',
                                'importance': 'DOUBLE',
                                'wikidata_id': 'VARCHAR'
                            }}
                        )
                        WHERE wikidata_id IS NOT NULL AND wikidata_id != ''
                        GROUP BY wikidata_id
                    ) w ON d.wikidata = w.wikidata_id
                ) WHERE {prune}
            ) TO '{output_str}' (FORMAT PARQUET, COMPRESSION ZSTD);
        """)

        # The read_csv column mapping is positional: a silent upstream
        # format change would leave every wiki_importance NULL and quietly
        # regress ranking fleet-wide. Fail loudly instead.
        matched = con.execute(f"""
            SELECT COUNT(*) FROM read_parquet('{output_str}')
            WHERE wiki_importance IS NOT NULL
        """).fetchone()[0]
        if matched == 0:
            raise RuntimeError(
                f"Wiki importance join matched 0 rows from {importance_file} — "
                "upstream format drift? Rerun with --no-wiki-importance to "
                "build without wikidata importance."
            )
    con.close()
    return output_path


# Columns selected from the (wiki-enriched) parquet for forward shard builds.
# Order matters: prepare_division_rows() unpacks positionally.
FORWARD_SHARD_SELECT = """
        gers_id,
        version,
        subtype as type,
        name,
        primary_name,
        lat,
        lon,
        bbox_xmin,
        bbox_ymin,
        bbox_xmax,
        bbox_ymax,
        population,
        country,
        region,
        search_name,
        search_context,
        class,
        wiki_importance,
        is_country_capital,
        is_region_capital
"""

# Index of the bbox_xmin column within FORWARD_SHARD_SELECT
BBOX_XMIN_INDEX = 7

DIVISIONS_INSERT_SQL = """
    INSERT INTO divisions (
        gers_id, version, type, primary_name, lat, lon,
        bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax,
        population, country, region,
        search_name, search_alias, search_context, importance
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def prepare_division_rows(rows: list[tuple]) -> list[tuple]:
    """
    Transform FORWARD_SHARD_SELECT rows into divisions-table insert tuples:
    derives search_alias (concatenations/abbreviations/designation strips)
    and the precomputed importance column.
    """
    prepared = []
    for row in rows:
        (gers_id, version, type_, name, primary_name, lat, lon,
         bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax,
         population, country, region,
         search_name, search_context, class_, wiki_importance,
         is_country_capital, is_region_capital) = row

        search_name = search_name or (name or primary_name).lower()
        search_alias = build_search_alias(name, search_name)
        # Coerce to float: DuckDB may surface DECIMAL for these, which the
        # sqlite3 driver cannot bind
        lat, lon = float(lat), float(lon)
        bbox_xmin, bbox_ymin = float(bbox_xmin), float(bbox_ymin)
        bbox_xmax, bbox_ymax = float(bbox_xmax), float(bbox_ymax)
        importance = compute_importance(
            type_,
            class_=class_,
            population=population,
            wiki_importance=wiki_importance,
            is_country_capital=bool(is_country_capital),
            is_region_capital=bool(is_region_capital),
        )
        prepared.append((
            gers_id, version, type_, primary_name, lat, lon,
            bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax,
            population, country, region,
            search_name, search_alias, search_context, importance,
        ))
    return prepared


def build_region_shard(
    parquet_path: Path,
    country_code: str,
    region_code: str,
    output_path: Path,
    version: str,
) -> dict:
    """
    Build SQLite shard for a single region within a country.

    Args:
        parquet_path: Path to the parquet file
        country_code: ISO 3166-1 alpha-2 country code (e.g., "CN")
        region_code: Full ISO 3166-2 region code (e.g., "CN-GD") or fallback (e.g., "CN-XX")
        output_path: Path to output SQLite database
        version: Version string for metadata

    Returns:
        Dict with region, record_count, size_bytes, bbox
    """
    # Validate inputs
    country_code = validate_country_code(country_code)
    region_code = validate_region_code(region_code)
    parquet_str = str(parquet_path.resolve())

    output_path.parent.mkdir(parents=True, exist_ok=True)

    if output_path.exists():
        output_path.unlink()

    con = duckdb.connect()
    db = sqlite3.connect(output_path)

    build_shard_schema(db)

    # Handle fallback region (null region records)
    is_fallback = region_code.endswith(f"-{FALLBACK_REGION_SUFFIX}")
    if is_fallback:
        region_filter = "region IS NULL"
    else:
        region_filter = f"region = '{region_code}'"

    # Query divisions for this region
    cursor = con.execute(f"""
        SELECT {FORWARD_SHARD_SELECT}
        FROM read_parquet('{parquet_str}')
        WHERE country = '{country_code}' AND {region_filter}
    """)

    # Stream rows in chunks
    count = 0
    bbox = [180.0, 90.0, -180.0, -90.0]  # [min_lon, min_lat, max_lon, max_lat]
    FETCH_SIZE = 50000

    while True:
        rows = cursor.fetchmany(FETCH_SIZE)
        if not rows:
            break

        # Update bbox from this batch
        for row in rows:
            b = BBOX_XMIN_INDEX
            bbox[0] = min(bbox[0], float(row[b]))      # bbox_xmin
            bbox[1] = min(bbox[1], float(row[b + 1]))  # bbox_ymin
            bbox[2] = max(bbox[2], float(row[b + 2]))  # bbox_xmax
            bbox[3] = max(bbox[3], float(row[b + 3]))  # bbox_ymax

        # Derive search_alias and precomputed importance
        prepared = prepare_division_rows(rows)

        db.executemany(DIVISIONS_INSERT_SQL, prepared)
        count += len(prepared)

    # Store metadata
    db.execute("INSERT OR REPLACE INTO metadata VALUES ('version', ?)", (version,))
    db.execute("INSERT OR REPLACE INTO metadata VALUES ('country', ?)", (country_code,))
    db.execute("INSERT OR REPLACE INTO metadata VALUES ('region', ?)", (region_code,))
    db.execute("INSERT OR REPLACE INTO metadata VALUES ('record_count', ?)", (str(count),))
    db.execute("INSERT OR REPLACE INTO metadata VALUES ('created_at', ?)",
               (datetime.now(timezone.utc).isoformat(),))

    # Optimize FTS and compact database
    db.execute("INSERT INTO divisions_fts(divisions_fts) VALUES('optimize')")
    db.commit()
    db.execute("VACUUM")
    db.close()
    con.close()

    return {
        "country": country_code,
        "region": region_code,
        "record_count": count,
        "size_bytes": output_path.stat().st_size,
        "bbox": bbox,
    }


def build_shard_schema(db: sqlite3.Connection):
    """Create the shard schema with FTS5."""
    db.executescript(f"""
        PRAGMA journal_mode=DELETE;
        PRAGMA synchronous=NORMAL;
        PRAGMA cache_size=-64000;

        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS divisions (
            rowid INTEGER PRIMARY KEY,
            gers_id TEXT NOT NULL UNIQUE,
            version INTEGER NOT NULL DEFAULT 0,
            type TEXT NOT NULL,
            primary_name TEXT NOT NULL,
            lat REAL NOT NULL,
            lon REAL NOT NULL,
            bbox_xmin REAL NOT NULL,
            bbox_ymin REAL NOT NULL,
            bbox_xmax REAL NOT NULL,
            bbox_ymax REAL NOT NULL,
            population INTEGER,
            country TEXT,
            region TEXT,
            search_name TEXT NOT NULL,
            search_alias TEXT,
            search_context TEXT,
            importance REAL NOT NULL
        );

        -- Weighted FTS columns (P4): the query side passes per-column weights
        -- via bm25(divisions_fts, 4.0, 2.0, 1.0) so name hits outrank alias
        -- hits outrank context hits.
        CREATE VIRTUAL TABLE IF NOT EXISTS divisions_fts USING fts5(
            search_name,
            search_alias,
            search_context,
            content='divisions',
            content_rowid='rowid',
            tokenize='{FTS5_TOKENIZER}',
            prefix='{FTS5_PREFIX_LENGTHS}'
        );

        CREATE TRIGGER IF NOT EXISTS divisions_ai AFTER INSERT ON divisions BEGIN
            INSERT INTO divisions_fts(rowid, search_name, search_alias, search_context)
            VALUES (new.rowid, new.search_name, new.search_alias, new.search_context);
        END;

        CREATE TRIGGER IF NOT EXISTS divisions_ad AFTER DELETE ON divisions BEGIN
            INSERT INTO divisions_fts(divisions_fts, rowid, search_name, search_alias, search_context)
            VALUES ('delete', old.rowid, old.search_name, old.search_alias, old.search_context);
        END;

        CREATE TRIGGER IF NOT EXISTS divisions_au AFTER UPDATE ON divisions BEGIN
            INSERT INTO divisions_fts(divisions_fts, rowid, search_name, search_alias, search_context)
            VALUES ('delete', old.rowid, old.search_name, old.search_alias, old.search_context);
            INSERT INTO divisions_fts(rowid, search_name, search_alias, search_context)
            VALUES (new.rowid, new.search_name, new.search_alias, new.search_context);
        END;
    """)


def build_country_shard(
    parquet_path: Path,
    country_code: str,
    output_path: Path,
    version: str,
) -> dict:
    """Build SQLite shard for a single country."""
    # Validate inputs to prevent SQL injection
    country_code = validate_country_code(country_code)
    parquet_str = str(parquet_path.resolve())

    output_path.parent.mkdir(parents=True, exist_ok=True)

    if output_path.exists():
        output_path.unlink()

    con = duckdb.connect()
    db = sqlite3.connect(output_path)

    build_shard_schema(db)

    # Query divisions for this country
    # Note: DuckDB requires file paths in the query string, but country_code is validated above
    cursor = con.execute(f"""
        SELECT {FORWARD_SHARD_SELECT}
        FROM read_parquet('{parquet_str}')
        WHERE country = '{country_code}'
    """)

    # Stream rows in chunks to avoid loading entire dataset into memory
    count = 0
    bbox = [180.0, 90.0, -180.0, -90.0]  # [min_lon, min_lat, max_lon, max_lat]
    FETCH_SIZE = 50000

    while True:
        rows = cursor.fetchmany(FETCH_SIZE)
        if not rows:
            break

        # Update bbox from this batch
        for row in rows:
            b = BBOX_XMIN_INDEX
            bbox[0] = min(bbox[0], float(row[b]))      # bbox_xmin
            bbox[1] = min(bbox[1], float(row[b + 1]))  # bbox_ymin
            bbox[2] = max(bbox[2], float(row[b + 2]))  # bbox_xmax
            bbox[3] = max(bbox[3], float(row[b + 3]))  # bbox_ymax

        # Derive search_alias and precomputed importance
        prepared = prepare_division_rows(rows)

        db.executemany(DIVISIONS_INSERT_SQL, prepared)
        count += len(prepared)

    # Store metadata
    db.execute("INSERT OR REPLACE INTO metadata VALUES ('version', ?)", (version,))
    db.execute("INSERT OR REPLACE INTO metadata VALUES ('country', ?)", (country_code,))
    db.execute("INSERT OR REPLACE INTO metadata VALUES ('record_count', ?)", (str(count),))
    db.execute("INSERT OR REPLACE INTO metadata VALUES ('created_at', ?)",
               (datetime.now(timezone.utc).isoformat(),))

    # Optimize FTS and compact database for WASM deserialize compatibility
    db.execute("INSERT INTO divisions_fts(divisions_fts) VALUES('optimize')")
    db.commit()
    db.execute("VACUUM")
    db.close()
    con.close()

    return {
        "country": country_code,
        "record_count": count,
        "size_bytes": output_path.stat().st_size,
        "bbox": bbox,
    }


def build_head_shard(
    parquet_path: Path,
    output_path: Path,
    version: str,
    population_threshold: int = DEFAULT_HEAD_THRESHOLD,
) -> dict:
    """Build HEAD shard with countries, regions, and large cities."""
    # Validate inputs
    population_threshold = validate_population_threshold(population_threshold)
    parquet_str = str(parquet_path.resolve())

    output_path.parent.mkdir(parents=True, exist_ok=True)

    if output_path.exists():
        output_path.unlink()

    con = duckdb.connect()
    db = sqlite3.connect(output_path)

    build_shard_schema(db)

    # HEAD shard query: countries, regions, high-population localities, and
    # famous-but-small places (high wikimedia importance) so they are globally
    # searchable without loading a country shard.
    # Note: population_threshold is validated as integer above
    cursor = con.execute(f"""
        SELECT {FORWARD_SHARD_SELECT}
        FROM read_parquet('{parquet_str}')
        WHERE population >= {population_threshold}
           OR subtype IN ('country', 'region')
           OR wiki_importance >= {HEAD_WIKI_IMPORTANCE_THRESHOLD}
    """)

    # Stream rows in chunks to avoid loading entire dataset into memory
    count = 0
    FETCH_SIZE = 50000

    while True:
        rows = cursor.fetchmany(FETCH_SIZE)
        if not rows:
            break

        # Derive search_alias and precomputed importance
        prepared = prepare_division_rows(rows)

        db.executemany(DIVISIONS_INSERT_SQL, prepared)
        count += len(prepared)

    # Store metadata
    db.execute("INSERT OR REPLACE INTO metadata VALUES ('version', ?)", (version,))
    db.execute("INSERT OR REPLACE INTO metadata VALUES ('type', ?)", ("head",))
    db.execute("INSERT OR REPLACE INTO metadata VALUES ('population_threshold', ?)",
               (str(population_threshold),))
    db.execute("INSERT OR REPLACE INTO metadata VALUES ('record_count', ?)", (str(count),))
    db.execute("INSERT OR REPLACE INTO metadata VALUES ('created_at', ?)",
               (datetime.now(timezone.utc).isoformat(),))

    # Optimize FTS and compact database for WASM deserialize compatibility
    db.execute("INSERT INTO divisions_fts(divisions_fts) VALUES('optimize')")
    db.commit()
    db.execute("VACUUM")
    db.close()
    con.close()

    return {
        "country": "HEAD",
        "record_count": count,
        "size_bytes": output_path.stat().st_size,
        "bbox": [-180.0, -90.0, 180.0, 90.0],
    }


def build_reverse_shard_schema(db: sqlite3.Connection):
    """Create the reverse geocoding shard schema (no FTS, bbox-indexed)."""
    db.executescript("""
        PRAGMA journal_mode=DELETE;
        PRAGMA synchronous=NORMAL;
        PRAGMA cache_size=-64000;

        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        -- Reverse geocoding table: gers_id NOT UNIQUE to allow antimeridian splits
        CREATE TABLE IF NOT EXISTS divisions_reverse (
            rowid INTEGER PRIMARY KEY,
            gers_id TEXT NOT NULL,
            subtype TEXT NOT NULL,
            primary_name TEXT NOT NULL,
            lat REAL NOT NULL,
            lon REAL NOT NULL,
            bbox_xmin REAL NOT NULL,
            bbox_ymin REAL NOT NULL,
            bbox_xmax REAL NOT NULL,
            bbox_ymax REAL NOT NULL,
            area REAL NOT NULL,
            population INTEGER,
            country TEXT,
            region TEXT
        );

        -- R*Tree spatial index for reverse geocoding point-in-bbox queries.
        -- The reader (geocoder-core) detects this table and uses it; legacy
        -- shards without it fall back to a bbox range scan. Populated after
        -- data load by populate_reverse_rtree().
        CREATE VIRTUAL TABLE IF NOT EXISTS divisions_reverse_rtree USING rtree(
            id, xmin, xmax, ymin, ymax
        );

        -- Index for deduplication of antimeridian splits
        CREATE INDEX IF NOT EXISTS idx_gers_id ON divisions_reverse(gers_id);

        -- Area index for sorting by specificity
        CREATE INDEX IF NOT EXISTS idx_area ON divisions_reverse(area);
    """)


def populate_reverse_rtree(db: sqlite3.Connection):
    """Fill the R*Tree from divisions_reverse rows. Call after data load."""
    db.executescript("""
        DELETE FROM divisions_reverse_rtree;
        INSERT INTO divisions_reverse_rtree
            SELECT rowid, bbox_xmin, bbox_xmax, bbox_ymin, bbox_ymax
            FROM divisions_reverse;
    """)


def build_reverse_country_shard(
    parquet_path: Path,
    country_code: str,
    output_path: Path,
    version: str,
) -> dict:
    """Build reverse geocoding SQLite shard for a single country."""
    # Validate inputs
    country_code = validate_country_code(country_code)
    parquet_str = str(parquet_path.resolve())

    output_path.parent.mkdir(parents=True, exist_ok=True)

    if output_path.exists():
        output_path.unlink()

    con = duckdb.connect()
    db = sqlite3.connect(output_path)

    build_reverse_shard_schema(db)

    # Query reverse geocoding data for this country
    cursor = con.execute(f"""
        SELECT
            gers_id,
            subtype,
            primary_name,
            lat,
            lon,
            bbox_xmin,
            bbox_ymin,
            bbox_xmax,
            bbox_ymax,
            area,
            population,
            country,
            region
        FROM read_parquet('{parquet_str}')
        WHERE country = '{country_code}'
    """)

    count = 0
    bbox = [180.0, 90.0, -180.0, -90.0]
    FETCH_SIZE = 50000

    while True:
        rows = cursor.fetchmany(FETCH_SIZE)
        if not rows:
            break

        for row in rows:
            # Update bbox
            bbox[0] = min(bbox[0], row[5])  # bbox_xmin
            bbox[1] = min(bbox[1], row[6])  # bbox_ymin
            bbox[2] = max(bbox[2], row[7])  # bbox_xmax
            bbox[3] = max(bbox[3], row[8])  # bbox_ymax

        db.executemany("""
            INSERT INTO divisions_reverse (
                gers_id, subtype, primary_name, lat, lon,
                bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax,
                area, population, country, region
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, rows)
        count += len(rows)

    populate_reverse_rtree(db)

    # Store metadata
    db.execute("INSERT OR REPLACE INTO metadata VALUES ('version', ?)", (version,))
    db.execute("INSERT OR REPLACE INTO metadata VALUES ('country', ?)", (country_code,))
    db.execute("INSERT OR REPLACE INTO metadata VALUES ('record_count', ?)", (str(count),))
    db.execute("INSERT OR REPLACE INTO metadata VALUES ('type', ?)", ("reverse",))
    db.execute("INSERT OR REPLACE INTO metadata VALUES ('created_at', ?)",
               (datetime.now(timezone.utc).isoformat(),))

    db.commit()
    db.execute("VACUUM")
    db.close()
    con.close()

    return {
        "country": country_code,
        "record_count": count,
        "size_bytes": output_path.stat().st_size,
        "bbox": bbox,
    }


def build_reverse_head_shard(
    parquet_path: Path,
    output_path: Path,
    version: str,
    population_threshold: int = DEFAULT_HEAD_THRESHOLD,
) -> dict:
    """Build reverse HEAD shard with countries, regions, and large cities."""
    population_threshold = validate_population_threshold(population_threshold)
    parquet_str = str(parquet_path.resolve())

    output_path.parent.mkdir(parents=True, exist_ok=True)

    if output_path.exists():
        output_path.unlink()

    con = duckdb.connect()
    db = sqlite3.connect(output_path)

    build_reverse_shard_schema(db)

    # HEAD shard: countries, regions, counties, and high-population localities
    cursor = con.execute(f"""
        SELECT
            gers_id,
            subtype,
            primary_name,
            lat,
            lon,
            bbox_xmin,
            bbox_ymin,
            bbox_xmax,
            bbox_ymax,
            area,
            population,
            country,
            region
        FROM read_parquet('{parquet_str}')
        WHERE subtype IN ('country', 'region', 'county')
           OR (population IS NOT NULL AND population >= {population_threshold})
    """)

    count = 0
    FETCH_SIZE = 50000

    while True:
        rows = cursor.fetchmany(FETCH_SIZE)
        if not rows:
            break

        db.executemany("""
            INSERT INTO divisions_reverse (
                gers_id, subtype, primary_name, lat, lon,
                bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax,
                area, population, country, region
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, rows)
        count += len(rows)

    populate_reverse_rtree(db)

    # Store metadata
    db.execute("INSERT OR REPLACE INTO metadata VALUES ('version', ?)", (version,))
    db.execute("INSERT OR REPLACE INTO metadata VALUES ('type', ?)", ("reverse-head",))
    db.execute("INSERT OR REPLACE INTO metadata VALUES ('population_threshold', ?)",
               (str(population_threshold),))
    db.execute("INSERT OR REPLACE INTO metadata VALUES ('record_count', ?)", (str(count),))
    db.execute("INSERT OR REPLACE INTO metadata VALUES ('created_at', ?)",
               (datetime.now(timezone.utc).isoformat(),))

    db.commit()
    db.execute("VACUUM")
    db.close()
    con.close()

    return {
        "country": "HEAD",
        "record_count": count,
        "size_bytes": output_path.stat().st_size,
        "bbox": [-180.0, -90.0, 180.0, 90.0],
    }


def hash_file(path: Path) -> str:
    """Calculate SHA256 hash of a file."""
    sha256 = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            sha256.update(chunk)
    return sha256.hexdigest()


def generate_stac_collection(
    version: str,
    shard_infos: dict[str, dict],
    shard_hashes: dict[str, str],
    shards_subdir: str = "shards",
    region_sharded: dict[str, list[str]] | None = None,
) -> dict:
    """
    Generate STAC Collection for all shards with embedded items.

    Args:
        version: Version string
        shard_infos: Dict of shard_id -> {record_count, size_bytes, bbox, ...}
        shard_hashes: Dict of shard_id -> sha256 hash
        shards_subdir: Subdirectory name for shard files
        region_sharded: Dict of country_code -> list of region shard IDs
                        e.g., {"CN": ["CN-GD", "CN-BJ", ...], "IN": [...]}
    """
    # Calculate overall bbox and temporal extent
    overall_bbox = [-180.0, -90.0, 180.0, 90.0]
    now = datetime.now(timezone.utc).isoformat()

    # Embed item metadata directly in collection (reduces R2 fetches)
    items = {}
    for shard_id, info in shard_infos.items():
        item_data = {
            "record_count": info["record_count"],
            "size_bytes": info["size_bytes"],
            "sha256": shard_hashes.get(shard_id, ""),
            "href": f"./{shards_subdir}/{shard_id}.db",
            "bbox": info["bbox"],
        }
        # Add parent_country for region shards
        if "region" in info:
            item_data["parent_country"] = info["country"]
        items[shard_id] = item_data

    collection = {
        "type": "Collection",
        "stac_version": "1.1.0",
        "stac_extensions": [],
        "id": f"geocoder-shards-{version}",
        "title": f"Overture Geocoder Shards {version}",
        "description": "Pre-built SQLite FTS5 shards for geocoding Overture Maps divisions data",
        "license": "CDLA-Permissive-2.0",
        "extent": {
            "spatial": {"bbox": [overall_bbox]},
            "temporal": {"interval": [[now, None]]},
        },
        "summaries": {
            "shard_count": len(shard_infos),
            "total_records": sum(s["record_count"] for s in shard_infos.values()),
            "total_size_bytes": sum(s["size_bytes"] for s in shard_infos.values()),
        },
        "items": items,  # Embedded item metadata
        "links": [
            {"rel": "root", "href": "../catalog.json", "type": "application/json"},
            {"rel": "parent", "href": "../catalog.json", "type": "application/json"},
            {"rel": "self", "href": "./collection.json", "type": "application/json"},
        ],
    }

    # Add region_sharded metadata if any countries were split
    if region_sharded:
        collection["region_sharded"] = region_sharded

    return collection


def generate_stac_catalog(versions: list[str], latest: str) -> dict:
    """Generate root STAC Catalog."""
    child_links = [
        {
            "rel": "child",
            "href": f"./{v}/collection.json",
            "type": "application/json",
            "title": f"Geocoder shards {v}",
            **({"latest": True} if v == latest else {}),
        }
        for v in sorted(versions, key=version_sort_key, reverse=True)
    ]

    return {
        "type": "Catalog",
        "stac_version": "1.1.0",
        "id": "geocoder-shards",
        "title": "Overture Geocoder Shards",
        "description": "STAC catalog for Overture geocoder SQLite shards",
        "links": [
            {"rel": "root", "href": "./catalog.json", "type": "application/json"},
            {"rel": "self", "href": "./catalog.json", "type": "application/json"},
            *child_links,
        ],
    }


def write_json(path: Path, data: dict):
    """Write JSON file with pretty formatting."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)


def build_forward_shards(args, version: str, version_dir: Path) -> dict:
    """Build forward geocoding shards (FTS-based)."""
    shards_subdir = version_dir / "shards"

    print(f"Building FORWARD geocoding shards for version {version}")
    print(f"Output directory: {version_dir}")
    print(f"HEAD threshold: {args.head_threshold:,}")
    print()

    # P1: produce a working parquet with wiki_importance joined in by QID
    # (all-NULL column with --no-wiki-importance, so the schema is uniform)
    if args.no_wiki_importance:
        print("Building without wikidata importance (--no-wiki-importance)")
        importance_file = None
    else:
        importance_file = ensure_wiki_importance_file(args.wiki_importance_file)
    print("Joining wikimedia importance into working parquet...")
    parquet = enrich_parquet_with_wiki_importance(
        args.parquet,
        args.parquet.with_name(args.parquet.stem + "-wiki" + args.parquet.suffix),
        importance_file,
    )

    shard_infos = {}

    # Build HEAD shard
    if not args.skip_head:
        print("Building HEAD shard...")
        head_path = shards_subdir / "HEAD.db"
        head_info = build_head_shard(
            parquet, head_path, version, args.head_threshold
        )
        shard_infos["HEAD"] = head_info
        print(f"  HEAD: {head_info['record_count']:,} records, "
              f"{head_info['size_bytes'] / 1024 / 1024:.1f} MB")

    # Track which countries are split into regions
    region_sharded = {}

    if args.head_only:
        print("\nHead-only mode, skipping country shards")
    else:
        # Get list of countries
        explicit_countries = None
        if args.countries:
            explicit_countries = [c.strip().upper() for c in args.countries.split(",")]

        # Single pass over the global parquet: partition by country locally,
        # then build every shard from its own (small) partition.
        print("\nPartitioning divisions by country (single pass)...")
        partition_dir = PARTITIONS_DIR / "forward"
        counts = partition_by_country(
            parquet, partition_dir, countries=explicit_countries
        )

        if explicit_countries:
            countries = explicit_countries
        else:
            countries = sorted(counts)
            print(f"Found {len(countries)} countries")

        # PASS 1: Build country shards
        print(f"\nPass 1: Building {len(countries)} country shards...")
        oversized_countries = []

        for i, country in enumerate(countries, 1):
            record_count = counts.get(country, 0)
            pct = 100 * i / len(countries)
            threshold_mb = SHARD_SIZE_THRESHOLD_BYTES / 1024 / 1024

            # Route obviously oversized countries straight to region builds:
            # building the full country shard (FTS index + VACUUM) only to
            # delete it again is wasted work.
            if record_count >= REGION_SPLIT_RECORD_THRESHOLD:
                oversized_countries.append(country)
                print(f"  [{i}/{len(countries)} {pct:.0f}%] {country}: "
                      f"{record_count:,} records, "
                      f"routing straight to region shards")
                continue

            # Countries absent from the partition (zero records) fall back to
            # the global parquet so an empty shard is still produced.
            source = (
                country_partition_glob(partition_dir, country)
                if record_count > 0 else parquet
            )
            output_path = shards_subdir / f"{country}.db"
            info = build_country_shard(source, country, output_path, version)

            size_mb = info['size_bytes'] / 1024 / 1024

            if info['size_bytes'] > SHARD_SIZE_THRESHOLD_BYTES:
                oversized_countries.append(country)
                print(f"  [{i}/{len(countries)} {pct:.0f}%] {country}: "
                      f"{info['record_count']:,} records, "
                      f"{size_mb:.1f} MB (OVERSIZED > {threshold_mb:.0f} MB)")
            else:
                shard_infos[country] = info
                print(f"  [{i}/{len(countries)} {pct:.0f}%] {country}: "
                      f"{info['record_count']:,} records, "
                      f"{size_mb:.1f} MB")

        # PASS 2: Rebuild oversized countries as region shards
        if oversized_countries:
            print(f"\nPass 2: Splitting {len(oversized_countries)} oversized countries into regions...")

            for country in oversized_countries:
                # Delete the oversized country shard (if it was built)
                country_shard_path = shards_subdir / f"{country}.db"
                if country_shard_path.exists():
                    country_shard_path.unlink()

                # Get regions for this country (from its local partition)
                source = country_partition_glob(partition_dir, country)
                regions = get_regions_for_country(source, country)
                print(f"\n  {country}: Splitting into {len(regions)} regions...")

                region_sharded[country] = []

                for region_code, record_count in regions:
                    output_path = shards_subdir / f"{region_code}.db"
                    info = build_region_shard(
                        source, country, region_code, output_path, version
                    )
                    shard_infos[region_code] = info
                    region_sharded[country].append(region_code)

                    size_mb = info['size_bytes'] / 1024 / 1024
                    if size_mb > SHARD_SIZE_THRESHOLD_BYTES / 1024 / 1024:
                        print(f"    {region_code}: {info['record_count']:,} records, "
                              f"{size_mb:.1f} MB (WARNING: still large)")
                    else:
                        print(f"    {region_code}: {info['record_count']:,} records, "
                              f"{size_mb:.1f} MB")

        # Free local scratch space (partitions can be several GB)
        shutil.rmtree(partition_dir, ignore_errors=True)

    # Calculate hashes for all shards
    print("\nCalculating shard hashes...")
    shard_hashes = {}
    for shard_id in shard_infos:
        shard_path = shards_subdir / f"{shard_id}.db"
        shard_hashes[shard_id] = hash_file(shard_path)

    # Generate collection with embedded items and region_sharded metadata
    print("Generating STAC catalog...")
    collection = generate_stac_collection(
        version, shard_infos, shard_hashes, "shards",
        region_sharded=region_sharded if region_sharded else None
    )
    write_json(version_dir / "collection.json", collection)

    return shard_infos


def build_reverse_shards(args, version: str, version_dir: Path) -> dict:
    """Build reverse geocoding shards (bbox-based)."""
    reverse_subdir = version_dir / "reverse"

    # Use reverse parquet if available, otherwise error
    parquet_path = args.parquet
    if args.parquet == DIVISIONS_PARQUET:
        parquet_path = DIVISIONS_REVERSE_PARQUET

    if not parquet_path.exists():
        print(f"Error: {parquet_path} not found")
        print("Run: ./scripts/download_divisions.sh")
        sys.exit(1)

    print(f"Building REVERSE geocoding shards for version {version}")
    print(f"Input: {parquet_path}")
    print(f"Output directory: {version_dir}")
    print(f"HEAD threshold: {args.head_threshold:,}")
    print()

    shard_infos = {}

    # Build HEAD shard for reverse
    if not args.skip_head:
        print("Building reverse HEAD shard...")
        head_path = reverse_subdir / "HEAD.db"
        head_info = build_reverse_head_shard(
            parquet_path, head_path, version, args.head_threshold
        )
        shard_infos["HEAD"] = head_info
        print(f"  HEAD: {head_info['record_count']:,} records, "
              f"{head_info['size_bytes'] / 1024 / 1024:.1f} MB")

    if args.head_only:
        print("\nHead-only mode, skipping country shards")
    else:
        # Get list of countries from reverse parquet
        explicit_countries = None
        if args.countries:
            explicit_countries = [c.strip().upper() for c in args.countries.split(",")]

        # Single pass over the reverse parquet: partition by country locally
        print("\nPartitioning reverse divisions by country (single pass)...")
        partition_dir = PARTITIONS_DIR / "reverse"
        counts = partition_by_country(
            parquet_path, partition_dir, countries=explicit_countries
        )

        if explicit_countries:
            countries = explicit_countries
        else:
            countries = sorted(counts)
            print(f"Found {len(countries)} countries")

        # Build country shards
        print(f"\nBuilding {len(countries)} reverse country shards...")

        for i, country in enumerate(countries, 1):
            source = (
                country_partition_glob(partition_dir, country)
                if counts.get(country, 0) > 0 else parquet_path
            )
            output_path = reverse_subdir / f"{country}.db"
            info = build_reverse_country_shard(source, country, output_path, version)
            shard_infos[country] = info
            pct = 100 * i / len(countries)
            print(f"  [{i}/{len(countries)} {pct:.0f}%] {country}: "
                  f"{info['record_count']:,} records, "
                  f"{info['size_bytes'] / 1024 / 1024:.1f} MB")

        # Free local scratch space
        shutil.rmtree(partition_dir, ignore_errors=True)

    # Calculate hashes for all reverse shards
    print("\nCalculating shard hashes...")
    shard_hashes = {}
    for shard_id in shard_infos:
        shard_path = reverse_subdir / f"{shard_id}.db"
        shard_hashes[shard_id] = hash_file(shard_path)

    # Generate reverse collection with embedded items
    print("Generating reverse STAC catalog...")
    collection = generate_stac_collection(version, shard_infos, shard_hashes, "reverse")
    collection["id"] = f"geocoder-reverse-shards-{version}"
    collection["title"] = f"Overture Reverse Geocoder Shards {version}"
    collection["description"] = "Pre-built SQLite shards for reverse geocoding Overture Maps divisions data"
    write_json(version_dir / "reverse-collection.json", collection)

    return shard_infos


def main():
    parser = argparse.ArgumentParser(description="Build geocoder shards")
    parser.add_argument("--version", help="Version string (default: date-based with suffix)")
    parser.add_argument("--version-suffix", default="0",
                        help="Version suffix (default: 0, use 1, 2, etc. for rebuilds)")
    parser.add_argument("--head-threshold", type=lambda s: int(float(s)), default=DEFAULT_HEAD_THRESHOLD,
                        help=f"Population threshold for HEAD shard (default: {DEFAULT_HEAD_THRESHOLD})")
    parser.add_argument("--countries", help="Comma-separated list of countries to build")
    parser.add_argument("--head-only", action="store_true", help="Build HEAD shard only")
    parser.add_argument("--skip-head", action="store_true", help="Skip HEAD shard")
    parser.add_argument("--parquet", type=Path, default=DIVISIONS_PARQUET,
                        help=f"Input parquet file (default: {DIVISIONS_PARQUET})")
    parser.add_argument("--reverse", action="store_true",
                        help="Build reverse geocoding shards (bbox-indexed, no FTS)")
    parser.add_argument("--no-wiki-importance", action="store_true",
                        help="Build without the wikimedia importance join "
                             "(importance falls back to type prior + population)")
    parser.add_argument("--wiki-importance-file", type=Path,
                        default=WIKIMEDIA_IMPORTANCE_FILE,
                        help="Local cache path for the wikimedia importance file "
                             f"(default: {WIKIMEDIA_IMPORTANCE_FILE}; downloaded "
                             "from nominatim.org when missing)")
    args = parser.parse_args()

    if not args.reverse and not args.parquet.exists():
        print(f"Error: {args.parquet} not found")
        print("Run: ./scripts/download_divisions.sh")
        sys.exit(1)

    version = args.version or get_version(args.version_suffix)
    version_dir = SHARDS_DIR / version

    if args.reverse:
        shard_infos = build_reverse_shards(args, version, version_dir)
        shard_type = "reverse"
        collection_file = "reverse-collection.json"
        shards_subdir = "reverse"
    else:
        shard_infos = build_forward_shards(args, version, version_dir)
        shard_type = "forward"
        collection_file = "collection.json"
        shards_subdir = "shards"

    # Update root catalog (only for forward shards, reverse has its own collection)
    if not args.reverse:
        existing_versions = [version]
        catalog_path = SHARDS_DIR / "catalog.json"
        if catalog_path.exists():
            with open(catalog_path) as f:
                old_catalog = json.load(f)
                for link in old_catalog.get("links", []):
                    if link.get("rel") == "child":
                        v = link["href"].split("/")[1]
                        if v not in existing_versions:
                            existing_versions.append(v)

        catalog = generate_stac_catalog(existing_versions, version)
        write_json(catalog_path, catalog)

    # Summary
    total_records = sum(s["record_count"] for s in shard_infos.values())
    total_size = sum(s["size_bytes"] for s in shard_infos.values())

    print(f"\nDone! ({shard_type} geocoding)")
    print(f"  Shards: {len(shard_infos)}")
    print(f"  Total records: {total_records:,}")
    print(f"  Total size: {total_size / 1024 / 1024:.1f} MB")
    print(f"\nOutput:")
    if not args.reverse:
        print(f"  {SHARDS_DIR}/catalog.json")
    print(f"  {version_dir}/{collection_file}")
    print(f"  {version_dir}/{shards_subdir}/*.db")


if __name__ == "__main__":
    main()
