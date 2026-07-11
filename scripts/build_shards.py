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
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unicodedata
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

# ---------------------------------------------------------------------------
# Places prototype constants
# ---------------------------------------------------------------------------
PLACES_CA_BBOX = {
    "xmin": -124.5,
    "xmax": -114.0,
    "ymin": 32.5,
    "ymax": 42.1,
}

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

# Router constants
ROUTER_TOKEN_MIN_LEN = 3
ROUTER_MAX_SHARDS_PER_TOKEN = 3


def _router_normalize(s: str) -> str:
    return ''.join(
        c for c in unicodedata.normalize('NFD', s) if not unicodedata.combining(c)
    ).lower()


def _router_tokenize(text: str | None) -> set[str]:
    if not text:
        return set()
    normalized = _router_normalize(text).replace(';', ' ')
    tokens: set[str] = set()
    cur: list[str] = []
    for ch in normalized:
        if ch.isalnum():
            cur.append(ch)
        else:
            if cur:
                tok = ''.join(cur)
                if len(tok) >= ROUTER_TOKEN_MIN_LEN and any(c.isalpha() for c in tok):
                    tokens.add(tok)
                cur = []
    if cur:
        tok = ''.join(cur)
        if len(tok) >= ROUTER_TOKEN_MIN_LEN and any(c.isalpha() for c in tok):
            tokens.add(tok)
    return tokens


def build_global_router(
    parquet_path: Path,
    output_path: Path,
    head_threshold: int = 100_000,
    version: str = "dev",
) -> dict:
    parquet_str = str(parquet_path.resolve())
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()

    token_map: dict[str, dict[str, float]] = {}

    con = duckdb.connect()

    try:
        cursor = con.execute(f"""
            SELECT
                subtype, class, population, wiki_importance,
                is_country_capital, is_region_capital,
                primary_name, search_name, country, region
            FROM read_parquet('{parquet_str}')
        """)
        enriched = True
    except Exception:
        print("  Router: enriched columns not found, falling back to raw parquet schema")
        cursor = con.execute(f"""
            SELECT
                subtype, class, population,
                CAST(NULL AS DOUBLE) AS wiki_importance,
                CAST(NULL AS BOOLEAN) AS is_country_capital,
                CAST(NULL AS BOOLEAN) AS is_region_capital,
                COALESCE(primary_name, name) AS primary_name,
                COALESCE(CAST(search_text AS VARCHAR), '') AS search_name,
                country, region
            FROM read_parquet('{parquet_str}')
        """)
        enriched = False

    FETCH_SIZE = 50000
    total_rows = 0
    kept_rows = 0

    while True:
        rows = cursor.fetchmany(FETCH_SIZE)
        if not rows:
            break
        for row in rows:
            total_rows += 1
            (subtype, class_, population, wiki_importance,
             is_country_capital, is_region_capital,
             primary_name, search_name, country, region) = row

            if subtype in ('country', 'region'):
                continue

            is_head_locality = False
            if subtype == 'locality':
                pop = population or 0
                wiki = wiki_importance or 0.0
                if pop >= head_threshold or wiki >= HEAD_WIKI_IMPORTANCE_THRESHOLD:
                    is_head_locality = True

            if is_head_locality:
                continue

            shard_ids: list[str] = []
            if region and not region.endswith(f"-{FALLBACK_REGION_SUFFIX}"):
                shard_ids.append(region)
            if country:
                if not shard_ids or country != region:
                    shard_ids.append(country)

            if not shard_ids:
                continue

            importance = compute_importance(
                subtype or "",
                class_=class_,
                population=population,
                wiki_importance=wiki_importance,
                is_country_capital=bool(is_country_capital),
                is_region_capital=bool(is_region_capital),
            )

            if importance < 0.10:
                continue

            if subtype == 'locality':
                pop = population or 0
                if pop < 1000 and importance < 0.15:
                    continue

            kept_rows += 1

            tokens = _router_tokenize(primary_name)
            if importance >= 0.30 and search_name:
                tokens |= _router_tokenize(search_name)

            if country:
                tokens.add(country.lower())
            if region:
                tokens.add(region.lower())
                if '-' in region:
                    parts = region.lower().split('-')
                    if len(parts[-1]) >= 2:
                        tokens.add(parts[-1])

            for token in tokens:
                shard_dict = token_map.setdefault(token, {})
                for sid in shard_ids:
                    prev = shard_dict.get(sid)
                    if prev is None or importance > prev:
                        shard_dict[sid] = importance

    print(f"  Router: scanned {total_rows:,} rows, kept {kept_rows:,} for routing")
    print(f"  Router: {len(token_map):,} unique tokens before pruning")

    for token in list(token_map.keys()):
        shards = token_map[token]
        if len(shards) > ROUTER_MAX_SHARDS_PER_TOKEN:
            top = sorted(shards.items(), key=lambda x: x[1], reverse=True)[:ROUTER_MAX_SHARDS_PER_TOKEN]
            token_map[token] = dict(top)

    total_pairs = sum(len(v) for v in token_map.values())
    print(f"  Router: {total_pairs:,} token->shard pairs after per-token top-{ROUTER_MAX_SHARDS_PER_TOKEN} pruning")

    con.close()

    db = sqlite3.connect(output_path)
    db.execute("PRAGMA journal_mode=DELETE;")
    db.execute("PRAGMA synchronous=NORMAL;")
    db.executescript("""
        CREATE TABLE router(
            token TEXT NOT NULL,
            shard_id TEXT NOT NULL,
            max_importance REAL NOT NULL,
            PRIMARY KEY(token, shard_id)
        );
    """)

    batch = []
    for token, shard_dict in token_map.items():
        for sid, imp in shard_dict.items():
            batch.append((token, sid, imp))

    db.executemany("INSERT INTO router VALUES (?, ?, ?)", batch)
    db.execute("CREATE INDEX idx_token ON router(token);")
    db.commit()
    db.execute("VACUUM")
    db.close()

    size_bytes = output_path.stat().st_size
    print(f"  Router: wrote {output_path} ({size_bytes / 1024 / 1024:.2f} MB)")
    return {
        "size_bytes": size_bytes,
        "token_count": len(token_map),
        "pair_count": total_pairs,
        "version": version,
    }


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
    # search_name separates distinct alt names with ';' (older data is a
    # plain token bag); alias variants operate on individual words either way.
    tokens = search_name.lower().replace(";", " ").split()
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

# HEAD shard includes countries, regions, and localities with pop >= threshold.
# Counties and local-admin divisions remain country/region-shard only so the
# globally loaded HEAD shard stays small.
DEFAULT_HEAD_THRESHOLD = 100_000

# Reverse data includes populated localities at this threshold. Keep this in
# sync with download_divisions_area.sql: the build summary makes the impact of
# changing that extraction bar visible before publishing a new release.
REVERSE_LOCALITY_POPULATION_THRESHOLD = 50_000

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


def get_reverse_input_metrics(
    parquet_path: Path,
    locality_population_threshold: int = REVERSE_LOCALITY_POPULATION_THRESHOLD,
) -> dict[str, int]:
    """Return the reverse input's division and stored-area-component counts.

    A reverse parquet row represents one candidate area component. This is
    deliberately different from the number of divisions: multipart cities,
    islands, and antimeridian splits yield multiple rows for one GERS ID.
    Reporting both prevents a population-threshold change from being judged
    only by its division count while silently multiplying R*Tree entries.
    """
    locality_population_threshold = validate_population_threshold(
        locality_population_threshold
    )
    parquet_str = str(parquet_path.resolve())
    con = duckdb.connect()
    metrics = con.execute(f"""
        WITH rows AS MATERIALIZED (
            SELECT gers_id, subtype, population
            FROM read_parquet('{parquet_str}')
        ),
        components AS (
            SELECT gers_id, COUNT(*) AS component_count
            FROM rows
            GROUP BY gers_id
        )
        SELECT
            COUNT(*) AS area_components,
            COUNT(DISTINCT gers_id) AS candidate_divisions,
            COUNT(*) FILTER (
                WHERE subtype = 'locality'
                  AND COALESCE(population, 0) >= {locality_population_threshold}
            ) AS eligible_locality_components,
            COUNT(DISTINCT gers_id) FILTER (
                WHERE subtype = 'locality'
                  AND COALESCE(population, 0) >= {locality_population_threshold}
            ) AS eligible_localities,
            COUNT(DISTINCT gers_id) FILTER (WHERE component_count > 1)
                AS multipart_divisions
        FROM rows
        JOIN components USING (gers_id)
    """).fetchone()
    con.close()
    return {
        "area_components": metrics[0],
        "candidate_divisions": metrics[1],
        "eligible_locality_components": metrics[2],
        "eligible_localities": metrics[3],
        "multipart_divisions": metrics[4],
    }


def print_reverse_input_metrics(
    metrics: dict[str, int],
    locality_population_threshold: int = REVERSE_LOCALITY_POPULATION_THRESHOLD,
):
    """Print reverse-input counts used to evaluate locality coverage and cost."""
    print("Reverse input summary:")
    print(f"  Candidate divisions: {metrics['candidate_divisions']:,}")
    print(f"  Stored area components: {metrics['area_components']:,}")
    print(f"  Multipart divisions: {metrics['multipart_divisions']:,}")
    print(
        "  Eligible populated localities "
        f"(population >= {locality_population_threshold:,}): "
        f"{metrics['eligible_localities']:,} divisions, "
        f"{metrics['eligible_locality_components']:,} area components"
    )


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


# Same-name localities in the same country+region within this distance are
# collapsed to one record (Overture ships duplicate GERS entities: Kissimmee
# FL x4, Randburg, Hoover... see docs/overture-data-feedback-2026-07-02.md).
# Genuinely distinct same-named places (Rosario PH x2, 74 km apart) sit far
# beyond this radius and are kept.
DEDUP_RADIUS_KM = 15.0


def dedup_localities(parquet_path: Path, output_path: Path) -> Path:
    """
    Collapse duplicate locality records: same (name, country, region) within
    DEDUP_RADIUS_KM of the group's leader (highest wiki importance, then
    population, then gers_id for determinism). The leader absorbs the
    cluster's max population, the union of its bboxes, and the union of its
    search_name segments; dropped GERS IDs are logged for auditability.
    """
    parquet_str = str(parquet_path.resolve())
    output_str = str(output_path.resolve())

    con = spill_safe_connect()
    con.execute("SET preserve_insertion_order = false;")
    con.execute(f"""
        CREATE TEMP TABLE ranked AS
        SELECT gers_id, lower(name) AS gname, country,
               coalesce(region, '') AS gregion,
               lat, lon, population, bbox_xmin, bbox_ymin, bbox_xmax,
               bbox_ymax, search_name,
               ROW_NUMBER() OVER (
                   PARTITION BY lower(name), country, coalesce(region, '')
                   ORDER BY coalesce(wiki_importance, 0) DESC,
                            coalesce(population, 0) DESC, gers_id
               ) AS rn
        FROM read_parquet('{parquet_str}')
        WHERE subtype = 'locality';
    """)
    # Duplicates: non-leaders within the radius of their group's leader.
    con.execute(f"""
        CREATE TEMP TABLE dupes AS
        SELECT r.gers_id, r.gname, r.country, r.gregion,
               l.gers_id AS leader_id
        FROM ranked r
        JOIN ranked l ON l.rn = 1 AND r.gname = l.gname
            AND r.country = l.country AND r.gregion = l.gregion
        WHERE r.rn > 1
          AND 2 * 6371 * ASIN(SQRT(
                POW(SIN(RADIANS(r.lat - l.lat) / 2), 2)
                + COS(RADIANS(l.lat)) * COS(RADIANS(r.lat))
                  * POW(SIN(RADIANS(r.lon - l.lon) / 2), 2)
              )) <= {DEDUP_RADIUS_KM};
    """)
    dropped = con.execute(
        "SELECT gname, country, gregion, gers_id, leader_id FROM dupes ORDER BY gname"
    ).fetchall()
    if dropped:
        print(f"  Deduplicating {len(dropped)} duplicate locality record(s):")
        for gname, country, gregion, gers_id, leader_id in dropped:
            print(f"    {gname} ({country}/{gregion or '-'}): "
                  f"dropping {gers_id} (kept {leader_id})")

    # Leaders absorb their cluster: max population, bbox union, and the
    # union of ';'-separated search_name segments.
    con.execute(f"""
        CREATE TEMP TABLE merged AS
        SELECT d.leader_id,
               MAX(GREATEST(coalesce(r.population, 0),
                            coalesce(l.population, 0))) AS population,
               LEAST(MIN(r.bbox_xmin), MIN(l.bbox_xmin)) AS bbox_xmin,
               LEAST(MIN(r.bbox_ymin), MIN(l.bbox_ymin)) AS bbox_ymin,
               GREATEST(MAX(r.bbox_xmax), MAX(l.bbox_xmax)) AS bbox_xmax,
               GREATEST(MAX(r.bbox_ymax), MAX(l.bbox_ymax)) AS bbox_ymax,
               ARRAY_TO_STRING(LIST_DISTINCT(FLATTEN(
                   LIST(STRING_SPLIT(r.search_name, ';'))
                   || LIST(STRING_SPLIT(l.search_name, ';'))
               )), ';') AS search_name
        FROM dupes d
        JOIN ranked r ON r.gers_id = d.gers_id
        JOIN ranked l ON l.gers_id = d.leader_id
        GROUP BY d.leader_id;
    """)
    con.execute(f"""
        COPY (
            SELECT src.* REPLACE (
                CASE WHEN m.population > 0 THEN m.population
                     ELSE src.population END AS population,
                coalesce(m.bbox_xmin, src.bbox_xmin) AS bbox_xmin,
                coalesce(m.bbox_ymin, src.bbox_ymin) AS bbox_ymin,
                coalesce(m.bbox_xmax, src.bbox_xmax) AS bbox_xmax,
                coalesce(m.bbox_ymax, src.bbox_ymax) AS bbox_ymax,
                coalesce(m.search_name, src.search_name) AS search_name
            )
            FROM read_parquet('{parquet_str}') src
            LEFT JOIN merged m ON src.gers_id = m.leader_id
            WHERE src.gers_id NOT IN (SELECT gers_id FROM dupes)
        ) TO '{output_str}' (FORMAT PARQUET, COMPRESSION ZSTD);
    """)
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
        WHERE subtype IN ('country', 'region')
           OR (
               subtype = 'locality'
               AND (
                   population >= {population_threshold}
                   OR wiki_importance >= {HEAD_WIKI_IMPORTANCE_THRESHOLD}
               )
           )
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
            region TEXT,
            wkb BLOB
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


def prepare_reverse_rows(rows: list[tuple]) -> list[tuple]:
    """Coerce DuckDB numeric values to SQLite-compatible Python primitives.

    Supports optional trailing wkb column (future build with exact geometry).
    """
    prepared = []
    for row in rows:
        has_wkb = len(row) > 13
        base = (
            row[0], row[1], row[2],
            *(float(value) for value in row[3:10]),
            int(row[10]) if row[10] is not None else None,
            row[11], row[12],
        )
        if has_wkb:
            wkb_val = row[13] if len(row) > 13 else None
            if wkb_val is not None and not isinstance(wkb_val, (bytes, bytearray, type(None))):
                try:
                    wkb_val = bytes(wkb_val)
                except Exception:
                    wkb_val = None
            prepared.append(base + (wkb_val,))
        else:
            prepared.append(base + (None,))
    return prepared


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

        prepared_rows = prepare_reverse_rows(rows)
        for row in prepared_rows:
            # Update bbox
            bbox[0] = min(bbox[0], row[5])  # bbox_xmin
            bbox[1] = min(bbox[1], row[6])  # bbox_ymin
            bbox[2] = max(bbox[2], row[7])  # bbox_xmax
            bbox[3] = max(bbox[3], row[8])  # bbox_ymax

        db.executemany("""
            INSERT INTO divisions_reverse (
                gers_id, subtype, primary_name, lat, lon,
                bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax,
                area, population, country, region, wkb
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, prepared_rows)
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

        prepared_rows = prepare_reverse_rows(rows)
        db.executemany("""
            INSERT INTO divisions_reverse (
                gers_id, subtype, primary_name, lat, lon,
                bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax,
                area, population, country, region, wkb
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, prepared_rows)
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


def get_git_sha() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=Path(__file__).resolve().parent.parent,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def get_wiki_importance_sha(path: Path) -> str | None:
    if not path.exists() or path.stat().st_size == 0:
        return None
    try:
        return hash_file(path)
    except Exception:
        return None


def get_version_from_catalog(catalog_path: Path = SHARDS_DIR / "catalog.json") -> str | None:
    """Read latest version from a shards STAC catalog (local file)."""
    if not catalog_path.exists():
        return None
    try:
        with open(catalog_path) as f:
            catalog = json.load(f)
        for link in catalog.get("links", []):
            if link.get("rel") == "child" and link.get("latest") is True:
                href = link.get("href", "")
                parts = href.strip("./").split("/")
                if parts:
                    return parts[0]
        versions = []
        for link in catalog.get("links", []):
            if link.get("rel") == "child":
                href = link.get("href", "")
                parts = href.strip("./").split("/")
                if parts and parts[0]:
                    versions.append(parts[0])
        if versions:
            versions.sort(key=version_sort_key, reverse=True)
            return versions[0]
    except Exception:
        return None
    return None


def write_build_meta(
    version: str,
    version_dir: Path,
    shard_infos: dict[str, dict],
    args,
) -> Path:
    """Write shards/{version}/build-meta.json with reproducibility info."""
    is_places = bool(getattr(args, "places", False))
    overture_release = getattr(args, "overture_release", None)
    if overture_release:
        if is_places:
            division_s3_paths = []
            source_s3_paths = [
                f"s3://overturemaps-us-west-2/release/{overture_release}/theme=places/type=place/*"
            ]
        else:
            division_s3_paths = [
                f"s3://overturemaps-us-west-2/release/{overture_release}/theme=divisions/type=division/*",
                f"s3://overturemaps-us-west-2/release/{overture_release}/theme=divisions/type=division_area/*",
            ]
            source_s3_paths = division_s3_paths.copy()
    else:
        division_s3_paths = []
        source_s3_paths = []

    wiki_file = getattr(args, "wiki_importance_file", WIKIMEDIA_IMPORTANCE_FILE)
    if isinstance(wiki_file, str):
        wiki_file = Path(wiki_file)

    parquet_path = (
        getattr(args, "places_parquet", Path("exports/places-CA.parquet"))
        if is_places
        else getattr(args, "parquet", DIVISIONS_PARQUET)
    )
    if bool(getattr(args, "reverse", False)) and parquet_path == DIVISIONS_PARQUET:
        parquet_path = DIVISIONS_REVERSE_PARQUET
    input_size = None
    try:
        if parquet_path.exists():
            input_size = parquet_path.stat().st_size
    except Exception:
        pass

    meta = {
        "version": version,
        "build_timestamp": datetime.now(timezone.utc).isoformat(),
        "overture_release": overture_release,
        "source_s3_paths": source_s3_paths,
        "division_s3_paths": division_s3_paths,
        "wikimedia_importance": {
            "url": WIKIMEDIA_IMPORTANCE_URL,
            "local_path": str(wiki_file),
            "sha256": get_wiki_importance_sha(wiki_file),
        },
        "git_sha": get_git_sha(),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "duckdb_version": getattr(duckdb, "__version__", "unknown"),
        "thresholds": {
            "head_threshold": getattr(args, "head_threshold", DEFAULT_HEAD_THRESHOLD),
            "head_wiki_importance_threshold": HEAD_WIKI_IMPORTANCE_THRESHOLD,
            "wiki_locality_keep_threshold": WIKI_LOCALITY_KEEP_THRESHOLD,
            "shard_size_threshold_bytes": SHARD_SIZE_THRESHOLD_BYTES,
            "region_split_record_threshold": REGION_SPLIT_RECORD_THRESHOLD,
            "fts5_prefix_lengths": FTS5_PREFIX_LENGTHS,
            "fts5_tokenizer": FTS5_TOKENIZER,
            "router_token_min_len": ROUTER_TOKEN_MIN_LEN,
            "router_max_shards_per_token": ROUTER_MAX_SHARDS_PER_TOKEN,
            "dedup_radius_km": DEDUP_RADIUS_KM,
            "reverse_locality_population_threshold": REVERSE_LOCALITY_POPULATION_THRESHOLD,
        },
        "constants": {
            "type_prior": TYPE_PRIOR,
            "locality_class_prior": LOCALITY_CLASS_PRIOR,
            "wiki_importance_weight": WIKI_IMPORTANCE_WEIGHT,
            "pop_component_cap": POP_COMPONENT_CAP,
            "pop_component_coeff": POP_COMPONENT_COEFF,
        },
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
            "reverse": bool(getattr(args, "reverse", False)),
            "places": is_places,
            "no_wiki_importance": bool(getattr(args, "no_wiki_importance", False)),
            "no_router": bool(getattr(args, "no_router", False)),
            "countries": getattr(args, "countries", None),
            "head_only": bool(getattr(args, "head_only", False)),
            "skip_head": bool(getattr(args, "skip_head", False)),
        },
    }

    out_path = version_dir / "build-meta.json"
    write_json(out_path, meta)
    print(f"\nBuild meta: {out_path}")
    return out_path


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

    print("Deduplicating same-name locality clusters...")
    parquet = dedup_localities(
        parquet,
        parquet.with_name(parquet.stem + "-dedup" + parquet.suffix),
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

    if getattr(args, 'no_router', False):
        print("\nSkipping global router build (--no-router)")
    elif args.head_only:
        print("\nSkipping global router in head-only mode")
    else:
        print("\nBuilding global token router...")
        router_path = version_dir / "router.db"
        router_info = build_global_router(
            parquet, router_path, args.head_threshold, version
        )
        router_hash = hash_file(router_path)
        print(f"  Router hash: {router_hash[:12]}...")
        collection_path = version_dir / "collection.json"
        if collection_path.exists():
            with open(collection_path) as f:
                existing = json.load(f)
            existing["router"] = {
                "href": "./router.db",
                "size_bytes": router_info["size_bytes"],
                "sha256": router_hash,
                "token_count": router_info["token_count"],
                "pair_count": router_info["pair_count"],
            }
            write_json(collection_path, existing)

    return shard_infos


def print_reverse_shard_summary(shard_infos: dict[str, dict]):
    """Print a size-focused summary after a reverse shard build.

    Reverse country shards are currently not split by region, so an oversized
    shard is a release-risk warning rather than something this build can fix
    automatically. The largest-shard list makes locality threshold changes
    comparable across releases without inspecting every country log line.
    """
    if not shard_infos:
        print("Reverse shard summary: no shards built")
        return

    threshold_mb = SHARD_SIZE_THRESHOLD_BYTES / 1024 / 1024
    total_records = sum(info["record_count"] for info in shard_infos.values())
    total_bytes = sum(info["size_bytes"] for info in shard_infos.values())
    oversized = [
        (shard_id, info) for shard_id, info in shard_infos.items()
        if info["size_bytes"] > SHARD_SIZE_THRESHOLD_BYTES
    ]

    print("\nReverse shard summary:")
    print(
        f"  {len(shard_infos):,} shards, {total_records:,} stored components, "
        f"{total_bytes / 1024 / 1024:.1f} MB total"
    )
    print("  Largest shards:")
    for shard_id, info in sorted(
        shard_infos.items(), key=lambda item: item[1]["size_bytes"], reverse=True
    )[:5]:
        print(
            f"    {shard_id}: {info['record_count']:,} components, "
            f"{info['size_bytes'] / 1024 / 1024:.1f} MB"
        )

    if oversized:
        labels = ", ".join(shard_id for shard_id, _ in oversized)
        print(
            f"  WARNING: {len(oversized)} reverse shard(s) exceed "
            f"{threshold_mb:.0f} MB: {labels}"
        )


def build_reverse_shards(args, version: str, version_dir: Path) -> dict:
    """Build bbox-based reverse-geocoding shards, including populated localities."""
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

    print_reverse_input_metrics(get_reverse_input_metrics(parquet_path))
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
        head_size_mb = head_info['size_bytes'] / 1024 / 1024
        head_warning = (
            f" (WARNING: exceeds {SHARD_SIZE_THRESHOLD_BYTES / 1024 / 1024:.0f} MB)"
            if head_info['size_bytes'] > SHARD_SIZE_THRESHOLD_BYTES else ""
        )
        print(f"  HEAD: {head_info['record_count']:,} records, "
              f"{head_size_mb:.1f} MB{head_warning}")

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
            size_mb = info['size_bytes'] / 1024 / 1024
            warning = (
                f" (WARNING: exceeds {SHARD_SIZE_THRESHOLD_BYTES / 1024 / 1024:.0f} MB)"
                if info['size_bytes'] > SHARD_SIZE_THRESHOLD_BYTES else ""
            )
            print(f"  [{i}/{len(countries)} {pct:.0f}%] {country}: "
                  f"{info['record_count']:,} records, {size_mb:.1f} MB{warning}")

        # Free local scratch space
        shutil.rmtree(partition_dir, ignore_errors=True)

    print_reverse_shard_summary(shard_infos)

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
    collection["description"] = (
        "Pre-built SQLite shards for bbox-based reverse geocoding of Overture "
        "administrative divisions and populated localities"
    )
    write_json(version_dir / "reverse-collection.json", collection)

    return shard_infos



# ---------------------------------------------------------------------------
# Places prototype
# ---------------------------------------------------------------------------

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
) -> dict:
    region_code = validate_region_code(region_code)
    if limit is not None and limit <= 0:
        raise ValueError("places limit must be greater than zero")

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
                ORDER BY {importance_expr} DESC,
                         confidence DESC NULLS LAST,
                         gers_id
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
        order_clause = (
            f"ORDER BY {importance_expr} DESC, confidence DESC NULLS LAST, id"
            if limit
            else ""
        )
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

            importance = compute_places_importance(
                confidence, brand_name, brand_wikidata, cat_primary, basic_cat
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
    db.execute("INSERT OR REPLACE INTO metadata VALUES ('record_count', ?)", (str(count),))
    db.execute("INSERT OR REPLACE INTO metadata VALUES ('created_at', ?)",
               (datetime.now(timezone.utc).isoformat(),))

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
    places_subdir = version_dir / "places"
    parquet_path = getattr(args, "places_parquet", Path("exports/places-CA.parquet"))
    region_code = validate_region_code(getattr(args, "places_region", "US-CA"))
    limit: int | None = getattr(args, "places_limit", None)
    if limit is not None and limit <= 0:
        raise ValueError("--places-limit must be greater than zero")

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

    output_path = places_subdir / f"{region_code}-places.db"
    print(f"Building places shard for {region_code} from {parquet_path}")
    if limit:
        print(f"  Sampling limit: {limit:,} most prominent places")

    info = build_places_shard(parquet_path, output_path, version, region_code=region_code, limit=limit)
    size_mb = info["size_bytes"] / 1024 / 1024
    print(f"  {region_code}: {info['record_count']:,} records, {size_mb:.1f} MB")

    shard_id = f"{region_code}-places"
    shard_hash = hash_file(output_path)
    collection = generate_stac_collection(
        version,
        {shard_id: info},
        {shard_id: shard_hash},
        "places",
    )
    collection["id"] = f"geocoder-places-shards-{version}"
    collection["title"] = f"Overture Places Shards {version}"
    collection["description"] = "Experimental places (POI) shards for CA prototype"
    write_json(version_dir / "places-collection.json", collection)

    # The worker reads collection.json for every forward request. Add the
    # experimental places item there when a forward collection already exists,
    # while retaining places-collection.json as the standalone build artifact.
    forward_collection_path = version_dir / "collection.json"
    if forward_collection_path.exists():
        with open(forward_collection_path) as f:
            forward_collection = json.load(f)
        forward_collection.setdefault("items", {})[shard_id] = collection["items"][shard_id]
        summaries = forward_collection.setdefault("summaries", {})
        items = forward_collection["items"]
        summaries["shard_count"] = len(items)
        summaries["total_records"] = sum(
            item.get("record_count", 0) for item in items.values()
        )
        summaries["total_size_bytes"] = sum(
            item.get("size_bytes", 0) for item in items.values()
        )
        write_json(forward_collection_path, forward_collection)

    return {shard_id: info}



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
    parser.add_argument("--no-router", action="store_true",
                        help="Skip building global token router (router.db)")
    parser.add_argument("--wiki-importance-file", type=Path,
                        default=WIKIMEDIA_IMPORTANCE_FILE,
                        help="Local cache path for the wikimedia importance file "
                             f"(default: {WIKIMEDIA_IMPORTANCE_FILE}; downloaded "
                             "from nominatim.org when missing)")
    parser.add_argument("--places", action="store_true",
                        help="Build places (POI) prototype shards")
    parser.add_argument("--places-region", type=str, default="US-CA",
                        help="Places region code e.g. US-CA (default: US-CA)")
    parser.add_argument("--places-parquet", type=Path, default=Path("exports/places-CA.parquet"),
                        help="Input parquet for places (flattened or raw Overture places)")
    parser.add_argument("--places-limit", type=int, default=None,
                        help="Sampling limit: top N places by composed prominence")
    parser.add_argument("--overture-release", type=str, default=None,
                        help="Overture release tag for build metadata and places S3 fallback "
                             "(e.g., 2026-06-17.0)")
    args = parser.parse_args()

    is_places = getattr(args, "places", False)
    if not is_places and not args.reverse and not args.parquet.exists():
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
    elif is_places:
        shard_infos = build_places_shards(args, version, version_dir)
        shard_type = "places"
        collection_file = "places-collection.json"
        shards_subdir = "places"
    else:
        shard_infos = build_forward_shards(args, version, version_dir)
        shard_type = "forward"
        collection_file = "collection.json"
        shards_subdir = "shards"

    # Update root catalog (only for forward shards, reverse has its own collection)
    if not args.reverse and not is_places:
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

    try:
        write_build_meta(version, version_dir, shard_infos, args)
    except Exception as exc:
        print(f"Warning: failed to write build-meta.json: {exc}", file=sys.stderr)

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
    print(f"  {version_dir}/build-meta.json")


if __name__ == "__main__":
    main()
